# sync_reading — Edge BLE Forwarder (Cassia X2000)

Serviço de borda (Python 3, stdlib) que roda no gateway Cassia X2000: recebe leituras
BLE, remonta frames fragmentados, decodifica e encaminha para o backend (Go) na nuvem.

```
Sensores BLE → Gateway Cassia X2000 → [este serviço] → Backend Cloud (HTTP POST)
```

## Conceitos

- **Fonte único:** todo o código vive em [`src/`](src/). Versão é uma **tag git**, não nome de pasta.
- **Ambiente é só config:** QA e PRD usam o **mesmo código**; muda apenas a config (`cloud_url`).
- **Build = empacotar:** Python não compila — o "build" mescla a config do ambiente,
  carimba a versão e gera um `.tar.gz`.
- **Segredo nunca no pacote:** a `api_key` fica **no gateway** e sobrevive a upgrades.

```
src/                  app.py, version.py, lib/
config/               config.base.json + config.qas.json + config.prd.json   (sem segredo)
packaging/            autorun.sh, delete_app.sh
scripts/build.sh      build.sh <qas|prd> [versão]
VERSION               fonte única da versão (ex.: 1.16.0)
dist/                 saída do build (ignorada pelo git)
```

---

## 1) Rodar localmente (dev)

```bash
cd src && python3 app.py     # sem dependências pip
```

---

## 2) Gerar um build (empacotar)

O build mescla `config/config.base.json` + `config/config.<env>.json`, carimba a
versão (do arquivo `VERSION` ou do argumento) e gera o tarball em `dist/`.

```bash
./scripts/build.sh qas            # usa a versão do arquivo VERSION
./scripts/build.sh prd 1.16.0     # ou passa a versão explícita
# → dist/sync_reading-1.16.0-qas.tar.gz
# → dist/sync_reading-1.16.0-prd.tar.gz
```

Cada ambiente carimba a versão e marca o nome nos logs (`service`):
`sync_reading_qas` / `sync_reading_prd` (e `sync_reading_dev` rodando do fonte).

> ⚠️ Não use mais `tar -zcvf ...` na mão. O `build.sh` garante config correta por
> ambiente, exclui lixo (`__pycache__`, logs) e carimba a versão.

---

## 3) Release oficial (QA → PRD via GitHub)

QA e PRD diferem só na config, então não há branch por ambiente. Fluxo:

```bash
# 1. ajuste a versão
echo "1.16.0" > VERSION
git commit -am "release 1.16.0"

# 2. crie e envie a tag
git tag v1.16.0
git push origin main --tags
```

A tag `v*` dispara o GitHub Actions ([`.github/workflows/release.yml`](.github/workflows/release.yml)),
que builda **qas + prd** e anexa os dois `.tar.gz` ao **GitHub Release**. Esse é o
artefato imutável e rastreável (release = tag = commit = artefato).

> Dia a dia/QA: rode `./scripts/build.sh qas` local e teste. Só crie tag para promover.

---

## 4) Deploy no gateway Cassia

Acesse: **Gateway UI > Container > Container Operation > Remote Login**
- Login: `cassia`
- Senha: `cassia-XXXXXX` (últimos 6 do MAC, ex.: `cassia-e59684`)

Ou por SSH:
```bash
ssh -p 20022 cassia@<IP_DO_GATEWAY>
```

O tarball contém `sync_reading/autorun.sh` e `sync_reading/opt/sync_reading/...`.
O `autorun.sh` usa caminhos **absolutos** (`/opt/sync_reading`), então os arquivos do
app precisam ficar exatamente em `/opt/sync_reading`.

```bash
# 1. enviar o tarball do ambiente certo (do dist/ ou de um GitHub Release)
scp -P 20022 dist/sync_reading-1.16.0-prd.tar.gz cassia@<IP_DO_GATEWAY>:/tmp/

# --- a partir daqui, dentro do gateway ---

# 2. extrair em /tmp
cd /tmp && tar -xzf sync_reading-1.16.0-prd.tar.gz

# 3. instalar os arquivos do app em /opt/sync_reading
cp -r /tmp/sync_reading/opt/sync_reading /opt/
cp /tmp/sync_reading/autorun.sh /tmp/sync_reading/delete_app.sh /opt/sync_reading/
chmod +x /opt/sync_reading/autorun.sh

# 4. (uma única vez por gateway) gravar o segredo — sobrevive a upgrades
mkdir -p /root/config/sync_reading
echo '{"api_key":"SUA_CHAVE_AQUI"}' > /root/config/sync_reading/config.json

# 5. iniciar o supervisor (auto-restart + rotação de log)
sh /opt/sync_reading/autorun.sh
```

No boot, o app loga a versão e o ambiente em execução:
```json
{"event":"boot","app_version":"1.16.0","build_env":"prd", ...}
```

---

## 5) Logs e captura BLE (no gateway)

```bash
cat /opt/sync_reading/logs/app.log     # JSONL, 1 objeto por linha
rm -f /opt/sync_reading/logs/app.log   # limpar
```

`autorun.sh` rotaciona em 5 MB, mantendo até 6 arquivos (`.log.1`–`.log.6`).

Captura BLE com `btmon`:
```bash
sudo btmon -t                                   # tempo real no console
sudo btmon -w /tmp/cap_syncble.btsnoop          # arquivo btsnoop (Wireshark)
sudo btmon -t | tee /tmp/captura.txt            # texto + console

# baixar para o Mac
scp -P 20022 cassia@<IP_DO_GATEWAY>:/tmp/cap_syncble.btsnoop ~/Downloads/
scp -P 20022 cassia@<IP_DO_GATEWAY>:/tmp/captura.txt ~/Downloads/
```
