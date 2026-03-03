
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