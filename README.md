# Email Simple Sender

Ferramenta simples para testar e-mails HTML. Suba um ZIP com o HTML e os assets (imagens), configure o SMTP e dispare.

## Como usar

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Como a porta 5000 pode estar ocupada no macOS (AirPlay Receiver), rode na porta 5001:

```bash
python -c "
import os, sys
sys.path.insert(0, '.')
os.makedirs('uploads', exist_ok=True)
from app import app
app.run(debug=True, host='0.0.0.0', port=5001)
"
```

Acesse `http://localhost:5001`, preencha os dados do SMTP, remetente, destinatário, faça upload do ZIP e envie.

## Configuração SMTP

**Gmail:**
- Host: `smtp.gmail.com` / Porta: `587` / TLS: ativado
- Use uma [App Password](https://myaccount.google.com/apppasswords) — não a senha normal da conta

**Teste local sem autenticação (Mailhog):**
```bash
brew install mailhog
mailhog
```
- Host: `localhost` / Porta: `1025` / TLS: desativado / Usuário e senha em branco
- Interface web dos e-mails capturados: `http://localhost:8025`

## Formato do ZIP

O ZIP deve conter:
- Um arquivo `.html` (preferencialmente `index.html` ou `email.html`)
- Imagens referenciadas no HTML (ex: `images/logo.png`)

As imagens locais são automaticamente convertidas para anexos inline (CID), garantindo compatibilidade com clientes de e-mail.
