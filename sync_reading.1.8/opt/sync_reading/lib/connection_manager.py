#!/usr/bin/env python3
"""
Connection Manager — arquitetura on-demand BLE com conexões paralelas.

Cada um dos 2 chips BLE do Cassia X2000 suporta múltiplas conexões simultâneas.
O manager conecta TODOS os sensores pendentes em paralelo, respeitando o limite
de `max_per_chip` conexões por chip.

Estado de slot:
  _chip_count[i]  — número de conexões ativas + in-flight no chip i
  _mac_to_chip    — mac → chip para sensores com slot alocado
  _in_flight      — conjunto de macs com chamada HTTP de connect em andamento

Cada chamada HTTP (connect/disconnect) roda em thread daemon própria para não
bloquear o loop de 50 ms.
"""
import json
import os
import threading
import time
from queue import Empty

from lib.adv_parser import parse_synchroone_adv
from lib.cassia_api import connect_device, disconnect_device, get_connected_nodes
from lib.gatt_session import gatt_setup
from lib.log import jlog

SERVICE = "sync_reading"
NUM_CHIPS = 2
_EPOCH_SYNC_INTERVAL = 7 * 24 * 3600  # 7 dias em segundos


class ConnectionManager:
    def __init__(
        self,
        gateway_api: str,
        scan_queue,
        tx_done_queue,
        stats,
        should_stop,
        connect_timeout: int = 5,
        assembly_timeout: int = 3,
        retry_margin: int = 5,
        max_per_chip: int = 20,
        addr_type: str = "random",
        rssi_cache: dict = None,
        epoch_sync_path: str = "",
    ):
        self.gateway_api = gateway_api
        self.scan_queue = scan_queue
        self.tx_done_queue = tx_done_queue
        self.stats = stats
        self.should_stop = should_stop
        self.connect_timeout = connect_timeout
        self.assembly_timeout = assembly_timeout
        self.retry_margin = retry_margin
        self.max_per_chip = max_per_chip
        self.addr_type = addr_type
        self.rssi_cache = rssi_cache if rssi_cache is not None else {}

        self.cooldown_seconds = connect_timeout * 6  # ~30s padrão com timeout=5
        self._lock = threading.Lock()
        self._epoch_sync_path = epoch_sync_path
        self._epoch_sync_lock = threading.Lock()
        self._epoch_sync_state: dict = self._load_epoch_sync_file()

        # Número de slots ocupados (ativo + in-flight) por chip
        self._chip_count = [0] * NUM_CHIPS

        # mac → chip para sensores com slot alocado
        self._mac_to_chip: dict = {}

        # Macs com chamada HTTP de connect ainda não concluída
        self._in_flight: set = set()

        # mac → {last_seq_collected, last_seq_seen, connected_at, last_rssi}
        self._sensor_state: dict = {}

        # Fila de candidatos a conectar, ordenada por (pending_count, rssi) desc
        self._connect_queue: list = []

    # ------------------------------------------------------------------
    # Estado por sensor
    # ------------------------------------------------------------------

    def _get_state(self, mac: str) -> dict:
        return self._sensor_state.setdefault(mac, {
            "last_seq_collected": None,
            "last_seq_seen": None,
            "current_seq_num": None,
            "connected_at": None,
            "last_disconnect_at": None,
            "last_rssi": None,
            "last_addr_type": None,
            "pending_frames": 1,
            "frames_received": 0,
        })

    # ------------------------------------------------------------------
    # Epoch sync — persiste last_sync por MAC em arquivo JSON
    # ------------------------------------------------------------------

    def _load_epoch_sync_file(self) -> dict:
        if not self._epoch_sync_path:
            return {}
        try:
            with open(self._epoch_sync_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _needs_epoch_sync(self, mac: str) -> bool:
        with self._epoch_sync_lock:
            last = self._epoch_sync_state.get(mac, 0)
        return (time.time() - last) >= _EPOCH_SYNC_INTERVAL

    def _mark_epoch_synced(self, mac: str):
        now = time.time()
        with self._epoch_sync_lock:
            self._epoch_sync_state[mac] = now
            if not self._epoch_sync_path:
                return
            try:
                tmp = self._epoch_sync_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._epoch_sync_state, f)
                os.replace(tmp, self._epoch_sync_path)
            except Exception as e:
                jlog(SERVICE, "WARN", "epoch_sync_save_error",
                     "Falha ao persistir epoch_sync.json", mac=mac, error=str(e))

    # ------------------------------------------------------------------
    # Gestão de slots de chip
    # ------------------------------------------------------------------

    def _total_active(self) -> int:
        return sum(self._chip_count)

    def _has_free_slot(self) -> bool:
        return any(c < self.max_per_chip for c in self._chip_count)

    def _alloc_slot(self, mac: str):
        """Aloca slot no chip com menos conexões. Retorna chip index ou None."""
        best_chip = None
        best_count = self.max_per_chip + 1
        for i, count in enumerate(self._chip_count):
            if count < self.max_per_chip and count < best_count:
                best_chip = i
                best_count = count
        if best_chip is None:
            return None
        self._chip_count[best_chip] += 1
        self._mac_to_chip[mac] = best_chip
        return best_chip

    def _free_slot(self, mac: str):
        """Libera o slot do mac. Retorna chip index ou None."""
        chip = self._mac_to_chip.pop(mac, None)
        if chip is not None:
            self._chip_count[chip] = max(0, self._chip_count[chip] - 1)
        return chip

    # ------------------------------------------------------------------
    # Limpeza de conexões pré-existentes (restart recovery)
    # ------------------------------------------------------------------

    def _startup_cleanup(self):
        """
        Desconecta todos os devices que ficaram conectados de uma execução anterior.
        Garante que _chip_count parte do zero real, não de um estado fantasma.
        Devices re-anunciam após o disconnect e são reconectados normalmente pelo manager.
        """
        try:
            nodes = get_connected_nodes(self.gateway_api, timeout=self.connect_timeout)
        except Exception as e:
            jlog(SERVICE, "WARN", "startup_cleanup_query_error",
                 "Não foi possível listar conexões ativas no startup; _chip_count pode estar inconsistente",
                 error=str(e))
            return

        if not nodes:
            jlog(SERVICE, "INFO", "startup_cleanup", "Nenhuma conexão pré-existente encontrada")
            return

        jlog(SERVICE, "INFO", "startup_cleanup",
             "Desconectando devices pré-existentes do restart anterior",
             count=len(nodes), macs=[n["mac"] for n in nodes])

        for node in nodes:
            mac = node["mac"]
            try:
                status, _ = disconnect_device(self.gateway_api, mac, timeout=self.connect_timeout)
                jlog(SERVICE, "INFO", "startup_disconnect_sent",
                     "Device desconectado no startup", mac=mac, chip_id=node["chip_id"],
                     http_status=status)
            except Exception as e:
                jlog(SERVICE, "WARN", "startup_disconnect_error",
                     "Erro ao desconectar device no startup", mac=mac, error=str(e))

    # ------------------------------------------------------------------
    # Loop principal
    # ------------------------------------------------------------------

    def run(self):
        self._startup_cleanup()
        while not self.should_stop():
            to_connect = []
            with self._lock:
                self._drain_scan_queue()
                self._drain_tx_done_queue()
                self._check_retry_timers()
                to_connect = self._collect_connect_batch()
                self.stats.set("connections_active", self._total_active())

            # Spawnar threads fora do lock para não segurar enquanto cria thread
            for candidate, chip in to_connect:
                t = threading.Thread(
                    target=self._connect_thread,
                    args=(candidate, chip),
                    daemon=True,
                )
                t.start()

            time.sleep(0.05)

    # ------------------------------------------------------------------
    # Processamento de scan events
    # ------------------------------------------------------------------

    def _drain_scan_queue(self):
        while True:
            try:
                evt = self.scan_queue.get_nowait()
            except Empty:
                break
            self._handle_scan_event(evt)

    def _handle_scan_event(self, evt: dict):
        mac = evt["mac"]
        adv_hex = evt.get("adv_data", "")
        rssi = evt.get("rssi")

        parsed = parse_synchroone_adv(adv_hex)
        if parsed is None:
            return

        state = self._get_state(mac)
        seq_num = parsed["seq_num"]

        if rssi is not None:
            state["last_rssi"] = rssi
            self.rssi_cache[mac] = rssi
        state["last_addr_type"] = evt.get("addr_type", self.addr_type)

        # Sensor voltou ao idle (pending_count=0): reseta dedup para que o próximo
        # ciclo de medição (mesmo pending_count) seja tratado como novo evento.
        if parsed["pending_count"] == 0:
            state["last_seq_collected"] = None
            state["last_seq_seen"] = None
            return

        if parsed.get("error_flag"):
            jlog(SERVICE, "WARN", "sensor_error_flag",
                 "Sensor reportou error_flag=1", mac=mac, seq_num=seq_num)
        if parsed.get("low_battery"):
            jlog(SERVICE, "WARN", "sensor_low_battery",
                 "Sensor reportou low_battery=1", mac=mac, seq_num=seq_num)

        # Já conectado ou com connect em andamento
        if mac in self._mac_to_chip or mac in self._in_flight:
            return

        # Cool-down pós-disconnect: sensor ainda reiniciando o stack BLE
        last_dc = state.get("last_disconnect_at")
        if last_dc is not None:
            elapsed = time.time() - last_dc
            if elapsed < self.cooldown_seconds:
                return

        # Dado já coletado com este seq_num no ciclo atual
        if state["last_seq_collected"] is not None and state["last_seq_collected"] == seq_num:
            return

        # Novo pending_count: enfileirar sem duplicatas
        if state["last_seq_seen"] != seq_num:
            state["last_seq_seen"] = seq_num
            existing = next((c for c in self._connect_queue if c["mac"] == mac), None)
            if existing is None:
                self._connect_queue.append({
                    "mac": mac,
                    "seq_num": seq_num,
                    "pending_count": parsed.get("pending_count", 1),
                    "rssi": rssi if rssi is not None else -100,
                    "uid": parsed.get("uid"),
                    "addr_type": evt.get("addr_type", self.addr_type),
                })
                self._connect_queue.sort(
                    key=lambda c: (c["pending_count"], c["rssi"]),
                    reverse=True,
                )

    # ------------------------------------------------------------------
    # Processamento de TX_DONE (sinalizado pelo assembler)
    # ------------------------------------------------------------------

    def _drain_tx_done_queue(self):
        while True:
            try:
                item = self.tx_done_queue.get_nowait()
            except Empty:
                break
            mac = (item.get("device") or "").upper()
            if mac:
                self._handle_tx_done(mac)

    def _handle_tx_done(self, mac: str):
        state = self._sensor_state.get(mac)
        if state is None:
            return

        state["frames_received"] += 1
        frames_received = state["frames_received"]
        pending_frames = state.get("pending_frames", 1)

        if frames_received < pending_frames:
            jlog(SERVICE, "INFO", "frame_collected",
                 "Frame coletado; aguardando próximos na mesma conexão",
                 mac=mac, frames_received=frames_received, pending_frames=pending_frames)
            state["connected_at"] = time.time()
            return

        chip = self._free_slot(mac)
        self._in_flight.discard(mac)

        if chip is not None:
            state["connected_at"] = None
            state["last_disconnect_at"] = time.time()
            state["last_seq_collected"] = None
            state["last_seq_seen"] = None
            state["frames_received"] = 0
            self.stats.inc("connections_completed", 1)

        gw = self.gateway_api
        timeout = self.connect_timeout

        def _do_disconnect(m=mac, c=chip, n=frames_received):
            try:
                status, _ = disconnect_device(gw, m, timeout=timeout)
                jlog(SERVICE, "INFO", "disconnect_sent",
                     "Sensor desconectado após todos os frames coletados",
                     mac=m, chip=c, frames_collected=n, http_status=status)
            except Exception as e:
                jlog(SERVICE, "WARN", "disconnect_error",
                     "Erro ao desconectar sensor", mac=m, chip=c, error=str(e))

        threading.Thread(target=_do_disconnect, daemon=True).start()

    # ------------------------------------------------------------------
    # Retry: sensores que não responderam dentro do timeout
    # ------------------------------------------------------------------

    def _check_retry_timers(self):
        now = time.time()

        for mac, state in list(self._sensor_state.items()):
            if state["connected_at"] is None:
                continue
            if mac not in self._mac_to_chip and mac not in self._in_flight:
                continue

            # connected_at é resetado após cada TX_DONE: o threshold cobre 1 frame
            retry_threshold = self.assembly_timeout + self.retry_margin

            age = now - state["connected_at"]
            if age < retry_threshold:
                continue

            chip = self._free_slot(mac)
            self._in_flight.discard(mac)
            state["connected_at"] = None

            jlog(SERVICE, "WARN", "connection_timeout_retry",
                 "TX timeout sem TX_DONE; forçando desconexão e retry",
                 mac=mac, chip=chip, age_s=round(age, 1),
                 retry_threshold=retry_threshold,
                 pending_frames=state.get("pending_frames", 1))

            gw = self.gateway_api
            timeout = self.connect_timeout

            def _do_force_disconnect(m=mac, c=chip):
                try:
                    disconnect_device(gw, m, timeout=timeout)
                except Exception:
                    pass

            threading.Thread(target=_do_force_disconnect, daemon=True).start()

            # Re-enfileirar com mesmo seq_num para retry
            if state["last_seq_seen"] is not None:
                existing = next((c for c in self._connect_queue if c["mac"] == mac), None)
                if existing is None:
                    self._connect_queue.append({
                        "mac": mac,
                        "seq_num": state["last_seq_seen"],
                        "pending_count": 1,
                        "rssi": state.get("last_rssi") or -100,
                        "addr_type": state.get("last_addr_type") or self.addr_type,
                    })

            self.stats.inc("connection_retries", 1)

    # ------------------------------------------------------------------
    # Coletar batch de conexões a fazer neste ciclo
    # ------------------------------------------------------------------

    def _collect_connect_batch(self) -> list:
        """
        Consome a fila de candidatos e aloca slots para todos que couberem agora.
        Retorna lista de (candidate, chip) — threads serão spawadas fora do lock.
        """
        to_connect = []

        while self._has_free_slot() and self._connect_queue:
            candidate = self._connect_queue.pop(0)
            mac = candidate["mac"]
            seq_num = candidate["seq_num"]
            state = self._get_state(mac)

            # Descartar se já conectado, in-flight, ou dado já coletado
            if mac in self._mac_to_chip or mac in self._in_flight:
                continue
            if state["last_seq_collected"] is not None and state["last_seq_collected"] == seq_num:
                continue

            chip = self._alloc_slot(mac)
            if chip is None:
                # Todos os chips cheios: devolver candidato e parar
                self._connect_queue.insert(0, candidate)
                break

            self._in_flight.add(mac)
            to_connect.append((candidate, chip))

        return to_connect

    # ------------------------------------------------------------------
    # Thread de conexão (HTTP blocking, roda fora do lock)
    # ------------------------------------------------------------------

    def _start_disconnect_thread(self, mac: str, chip: int):
        """Libera slot e dispara disconnect em thread daemon."""
        with self._lock:
            self._free_slot(mac)
            state = self._sensor_state.get(mac)
            if state:
                state["connected_at"] = None

        gw = self.gateway_api
        timeout = self.connect_timeout

        def _do(m=mac, c=chip):
            try:
                status, _ = disconnect_device(gw, m, timeout=timeout)
                jlog(SERVICE, "INFO", "disconnect_sent",
                     "Sensor desconectado", mac=m, chip=c, http_status=status)
            except Exception as e:
                jlog(SERVICE, "WARN", "disconnect_error",
                     "Erro ao desconectar sensor", mac=m, chip=c, error=str(e))

        threading.Thread(target=_do, daemon=True).start()

    def _connect_thread(self, candidate: dict, chip: int):
        mac = candidate["mac"]
        seq_num = candidate["seq_num"]

        # Passo 1: conexão BLE via Cassia API
        try:
            status, body = connect_device(
                self.gateway_api, mac, chip,
                timeout=self.connect_timeout,
                addr_type=candidate.get("addr_type", self.addr_type),
            )
        except Exception as e:
            jlog(SERVICE, "WARN", "connect_error",
                 "Erro HTTP ao conectar sensor", mac=mac, chip=chip, error=str(e))
            with self._lock:
                self._in_flight.discard(mac)
                self._free_slot(mac)
                # Permite re-enfileirar imediatamente no próximo scan event
                st = self._sensor_state.get(mac)
                if st:
                    st["last_seq_seen"] = None
            self.stats.inc("connection_errors", 1)
            return

        with self._lock:
            self._in_flight.discard(mac)
            if status >= 400:
                self._free_slot(mac)
                st = self._sensor_state.get(mac)
                if st:
                    st["last_seq_seen"] = None
                jlog(SERVICE, "WARN", "connect_failed",
                     "Falha na conexão via Cassia API",
                     mac=mac, chip=chip, http_status=status, body=body[:200])
                self.stats.inc("connection_errors", 1)
                return

        jlog(SERVICE, "INFO", "connect_sent",
             "Conexão BLE estabelecida; iniciando setup GATT",
             mac=mac, chip=chip, seq_num=seq_num,
             pending_count=candidate["pending_count"],
             rssi=candidate["rssi"],
             uid=candidate.get("uid"),
             addr_type=candidate.get("addr_type", self.addr_type),
             http_status=status)

        # Passo 2: setup GATT — autenticação, slot check e habilitar indications
        # Roda fora do lock (bloqueia por várias chamadas HTTP sequenciais)
        sync_epoch = self._needs_epoch_sync(mac)
        try:
            slot_ok = gatt_setup(
                self.gateway_api, mac,
                timeout=self.connect_timeout,
                addr_type=candidate.get("addr_type", self.addr_type),
                sync_epoch=sync_epoch,
            )
        except Exception as e:
            jlog(SERVICE, "WARN", "gatt_setup_error",
                 "Erro no setup GATT; desconectando sensor",
                 mac=mac, chip=chip, error=str(e))
            self._start_disconnect_thread(mac, chip)
            with self._lock:
                st = self._sensor_state.get(mac)
                if st:
                    st["last_seq_seen"] = None
            self.stats.inc("connection_errors", 1)
            return

        if not slot_ok:
            # Slot ainda ocupado (race pós-disconnect) — desconectar e re-enfileirar para retry
            self._start_disconnect_thread(mac, chip)
            with self._lock:
                existing = next((c for c in self._connect_queue if c["mac"] == mac), None)
                if existing is None:
                    self._connect_queue.insert(0, {
                        "mac": mac,
                        "seq_num": seq_num,
                        "pending_count": candidate.get("pending_count", 1),
                        "rssi": candidate.get("rssi", -100),
                        "uid": candidate.get("uid"),
                        "addr_type": candidate.get("addr_type", self.addr_type),
                    })
            self.stats.inc("connection_retries", 1)
            return

        if sync_epoch:
            self._mark_epoch_synced(mac)

        # Setup concluído — congelar pending_frames do advertising e iniciar janela
        with self._lock:
            state = self._sensor_state.get(mac)
            if state:
                state["connected_at"] = time.time()
                state["pending_frames"] = candidate.get("pending_count", 1)
                state["frames_received"] = 0
                state["current_seq_num"] = seq_num
        self.stats.inc("connections_initiated", 1)


def connection_manager_loop(
    gateway_api: str,
    scan_queue,
    tx_done_queue,
    stats,
    should_stop,
    connect_timeout: int = 5,
    assembly_timeout: int = 3,
    retry_margin: int = 5,
    max_per_chip: int = 20,
    addr_type: str = "random",
    rssi_cache: dict = None,
    epoch_sync_path: str = "",
):
    manager = ConnectionManager(
        gateway_api=gateway_api,
        scan_queue=scan_queue,
        tx_done_queue=tx_done_queue,
        stats=stats,
        should_stop=should_stop,
        connect_timeout=connect_timeout,
        assembly_timeout=assembly_timeout,
        retry_margin=retry_margin,
        max_per_chip=max_per_chip,
        addr_type=addr_type,
        rssi_cache=rssi_cache,
        epoch_sync_path=epoch_sync_path,
    )
    manager.run()
