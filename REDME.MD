
> Dica: mantenha seus arquivos em `/opt/...` para ficar organizado dentro do container.

### 2) "Compilar" (na prática: empacotar)
Python não compila. O passo de build é gerar o **tar.gz**.

Na raiz do projeto (onde existe a pasta `bletest.1.0/`):

```bash
tar -zcvf sync_reading.1.3.tar.gz sync_reading.1.3/
```

### 3) "Ver logs" (dentro do servidor Cássia)
Entrar no gateway > Container > Container Operation > Remote Login

Utilizar as credenciais:
- Login: cassia
- Pass: cassia-XXXXXX (últimos códigos do mac adress "cassia-e59684")

```bash
cat /opt/sync_reading/logs/app.log

rm -f /opt/sync_reading/logs/app.log

sudo btmon -t



# Connectar via terminal mac
ssh -p 20022 cassia@<IP_DO_GATEWAY>
ssh -p 20022 cassia@192.168.6.126

sudo btmon -w /tmp/cap_syncble.btsnoop
sudo btmon -t > /tmp/cap_sync.txt
sudo btmon -t | tee /tmp/captura.txt
sudo btmon -t | tee /tmp/cap_logs.txt
sudo btmon -t | tee /tmp/cap_logs4.txt

scp -P 20022 cassia@<IP_DO_GATEWAY>:/tmp/cap_syncble.btsnoop ~/Downloads/
scp -P 20022 cassia@192.168.6.126:/tmp/cap_sync.txt ~/Downloads/

scp -P 20022 cassia@192.168.6.126:/tmp/captura.txt ~/Downloads/

scp -P 20022 cassia@192.168.6.126:/tmp/cap_logs.txt ~/Downloads/

