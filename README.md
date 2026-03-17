# Email Simple Sender

Ferramenta simples para testar e-mails HTML. Suba um ZIP com o HTML e os assets (imagens), configure o SMTP e dispare.

## Como usar

```bash
pip install -r requirements.txt
python app.py
```

Acesse `http://localhost:5000`, preencha os dados do SMTP, remetente, destinatário, faça upload do ZIP e envie.

## Formato do ZIP

O ZIP deve conter:
- Um arquivo `.html` (preferencialmente `index.html` ou `email.html`)
- Imagens referenciadas no HTML (ex: `images/logo.png`)

As imagens locais são automaticamente convertidas para anexos inline (CID), garantindo compatibilidade com clientes de e-mail.
