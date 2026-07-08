# Bot de Desabafos para Discord 💬

Bot para Discord focado em um servidor de desabafos. Age como um ouvinte
empático, conversa naturalmente usando IA (Google Gemini com fallback
automático para Groq) e responde apenas em um canal específico,
mantendo memória de conversa isolada por usuário.

Feito para rodar tanto em PC (Windows/Linux/macOS) quanto no celular
via Termux (Android).

---

## Novidades da v2.0

Atualização incremental que manteve toda a arquitetura e o estilo de
código originais, adicionando resiliência e observabilidade:

- **Gerenciamento inteligente de contexto**: histórico antigo é
  condensado automaticamente em um resumo, com limite de contexto
  enviado à IA configurável (`memory.py`).
- **Fila assíncrona** (`asyncio.Queue`) dentro de `ai.py`: impede
  chamadas simultâneas às APIs de IA e preserva a ordem das respostas.
- **Retry com backoff exponencial** para timeouts e erros HTTP
  temporários (429/500/502/503/504), com tentativas configuráveis.
- **Circuit breaker por provedor**: após falhas consecutivas, o
  provedor é marcado como indisponível temporariamente e reativado
  automaticamente depois.
- **SQLite** (`database.py`) com migração automática, guardando
  blacklist, estatísticas e configurações persistentes, além de
  **backup automático** com rotação.
- **Cache em memória** para blacklist/configurações, reduzindo
  consultas ao banco.
- **Limpeza automática** de histórico inativo, cache e logs antigos.
- **Watchdog** (`scheduler.py`) monitorando Discord, banco e provedores
  de IA, registrando problemas sem derrubar o bot.
- **Comandos `/health`** (status de Discord, SQLite, Gemini, Groq,
  uptime e tempo médio de resposta) **e `/version`** (versão, build,
  provedor prioritário e banco utilizado).
- **Logs expandidos** com duração da requisição, tempo de IA, provedor
  usado, motivo de fallback e tamanho aproximado do contexto enviado.

Nenhuma dependência pesada foi adicionada (sem Redis, Postgres, MySQL,
Docker obrigatório ou painel web) — tudo continua rodando com recursos
gratuitos, inclusive no Termux.

---

## Índice

1. [Estrutura do projeto](#estrutura-do-projeto)
2. [Como instalar](#como-instalar)
3. [Como obter o Token do Discord](#como-obter-o-token-do-discord)
4. [Como obter a chave da IA](#como-obter-a-chave-da-ia)
5. [Como configurar](#como-configurar)
6. [Como iniciar o bot](#como-iniciar-o-bot)
7. [Como trocar de modelo de IA](#como-trocar-de-modelo-de-ia)
8. [Como mudar o canal de desabafos](#como-mudar-o-canal-de-desabafos)
9. [Como hospedar](#como-hospedar)
10. [Como rodar no Termux (celular)](#como-rodar-no-termux-celular)
11. [Como atualizar](#como-atualizar)
12. [Erros comuns e soluções](#erros-comuns-e-soluções)

---

## Estrutura do projeto

```
desabafos-bot/
├── main.py            # Ponto de entrada do bot
├── config.py          # Carrega e valida configurações do .env
├── ai.py              # IA (Gemini + fallback Groq, fila, retry, circuit breaker)
├── prompts.py          # Prompt de sistema / personalidade da IA
├── memory.py           # Histórico de conversa isolado por usuário + resumo automático
├── database.py         # SQLite: blacklist, estatísticas, config persistente, backup
├── scheduler.py         # Tarefas periódicas: backup, limpeza e watchdog
├── version.py           # Versão e data de build (usados no /version)
├── events.py           # Eventos do Discord + comandos /health e /version
├── logger.py           # Sistema de logs coloridos (terminal + arquivo)
├── utils.py            # Cooldown, validação de mensagens, backoff e detecção de erros
├── requirements.txt    # Dependências do projeto
├── .env.example        # Modelo de variáveis de ambiente
├── dados/               # Criado automaticamente: banco SQLite, backups e logs
└── .gitignore
```

---

## Como instalar

### Pré-requisitos

- Python 3.12 ou superior
- Uma conta no Discord com permissão para criar aplicações/bots
- Uma chave de API gratuita do Google Gemini (e, opcionalmente, da Groq)

### Passos

```bash
# 1. Clone ou copie o projeto
cd desabafos-bot

# 2. Crie um ambiente virtual (recomendado)
python3 -m venv venv

# 3. Ative o ambiente virtual
# Linux/macOS:
source venv/bin/activate
# Windows (PowerShell):
venv\Scripts\Activate.ps1

# 4. Instale as dependências
pip install -r requirements.txt

# 5. Copie o arquivo de exemplo de variáveis de ambiente
cp .env.example .env
```

---

## Como obter o Token do Discord

1. Acesse o [Discord Developer Portal](https://discord.com/developers/applications).
2. Clique em **New Application** e dê um nome ao seu bot.
3. No menu lateral, vá em **Bot**.
4. Clique em **Reset Token** (ou **Add Bot**, se ainda não existir) e copie o token gerado.
5. Ative a opção **Message Content Intent** na mesma página (obrigatório para o bot ler mensagens).
6. Cole o token no seu `.env`, na variável `TOKEN_DISCORD`.
7. Para convidar o bot ao seu servidor, vá em **OAuth2 > URL Generator**, marque `bot`, selecione as permissões `Read Messages/View Channels`, `Send Messages` e `Read Message History`, e acesse a URL gerada.

---

## Como obter a chave da IA

### Google Gemini (prioridade 1, gratuito)

1. Acesse [Google AI Studio](https://aistudio.google.com/app/apikey).
2. Faça login com uma conta Google.
3. Clique em **Create API Key**.
4. Copie a chave e cole em `API_KEY_GEMINI` no `.env`.

### Groq (fallback, gratuito)

1. Acesse [console.groq.com/keys](https://console.groq.com/keys).
2. Crie uma conta ou faça login.
3. Gere uma nova chave de API.
4. Cole em `API_KEY_GROQ` no `.env`.

> Você pode configurar apenas uma das duas chaves. Se ambas estiverem
> configuradas, o bot tenta primeiro o Gemini e usa a Groq
> automaticamente caso o Gemini falhe ou fique indisponível.

---

## Como configurar

Edite o arquivo `.env` com seus dados:

```env
TOKEN_DISCORD=seu_token_aqui
CANAL_DESABAFOS=123456789012345678

API_KEY_GEMINI=sua_chave_gemini
MODEL_NAME=gemini-1.5-flash

API_KEY_GROQ=sua_chave_groq
GROQ_MODEL_NAME=llama-3.1-70b-versatile

TEMPERATURE=0.9
MAX_HISTORY=10

COOLDOWN_SEGUNDOS=5
TAMANHO_MAXIMO_MENSAGEM=1500
```

Para obter o ID do canal (`CANAL_DESABAFOS`): ative o **Modo
Desenvolvedor** no Discord (Configurações > Avançado), clique com o
botão direito no canal desejado e selecione **Copiar ID**.

> A v2.0 adicionou variáveis extras (fila, retry, circuit breaker,
> SQLite, limpeza automática, watchdog), todas com valores padrão
> sensatos. Veja a lista completa e comentada em `.env.example` — só
> precisa ajustar se quiser mudar o comportamento padrão.

---

## Como iniciar o bot

```bash
python main.py
```

Se tudo estiver correto, você verá no terminal:

```
[12:00:00] INFO     | Iniciando Bot de Desabafos...
[12:00:02] INFO     | Bot conectado como SeuBot#0000 em 1 servidor(es).
```

---

## Como trocar de modelo de IA

Basta alterar a variável `MODEL_NAME` (para Gemini) ou `GROQ_MODEL_NAME`
(para Groq) no `.env`. Exemplos de modelos Gemini disponíveis:
`gemini-1.5-flash`, `gemini-1.5-pro`, `gemini-2.0-flash`. Consulte a
documentação oficial de cada provedor para a lista atualizada de
modelos disponíveis, pois isso muda com frequência.

Para adicionar um provedor totalmente novo no futuro, crie um método
`_gerar_com_<provedor>` em `ai.py` e adicione-o à lista de tentativas
em `gerar_resposta`.

---

## Como mudar o canal de desabafos

Altere o valor de `CANAL_DESABAFOS` no `.env` para o ID do novo canal
e reinicie o bot.

---

## Como hospedar

Qualquer serviço que rode processos Python 24/7 funciona, por exemplo:

- **VPS (Oracle Cloud Free Tier, Hetzner, etc.)**: instale Python,
  clone o projeto, configure o `.env` e rode com um gerenciador de
  processos como `systemd`, `pm2` ou `screen`/`tmux` para manter o bot
  ativo mesmo após fechar o terminal.
- **Railway / Render**: crie um novo serviço apontando para o
  repositório, defina as variáveis de ambiente no painel e configure
  o comando de start como `python main.py`.

Exemplo de execução persistente com `tmux`:

```bash
tmux new -s desabafos-bot
python main.py
# Ctrl+B, depois D para sair sem encerrar o processo
```

---

## Como rodar no Termux (celular)

```bash
# Atualize os pacotes
pkg update && pkg upgrade

# Instale Python e git
pkg install python git

# Clone/copie o projeto para o celular
cd desabafos-bot

# Instale as dependências
pip install -r requirements.txt

# Configure o .env (use um editor de texto do Termux, como nano)
cp .env.example .env
nano .env

# Inicie o bot
python main.py
```

Para manter o bot rodando com a tela do celular apagada, use
`termux-wake-lock` (parte do pacote `termux-api`) antes de iniciar o bot.

---

## Como atualizar

```bash
git pull            # se estiver usando git
pip install -r requirements.txt --upgrade
```

Revise o `.env.example` após atualizar: novas variáveis podem ter sido
adicionadas, e você precisará copiá-las para o seu `.env` existente.

---

## Erros comuns e soluções

| Erro | Causa provável | Solução |
|---|---|---|
| `discord.LoginFailure` | Token inválido ou expirado | Gere um novo token no Developer Portal e atualize o `.env` |
| Bot não responde a nenhuma mensagem | `CANAL_DESABAFOS` incorreto, ou `Message Content Intent` desativado | Confira o ID do canal e ative o intent no Developer Portal |
| `ErroDeIA: Nenhum provedor de IA conseguiu responder` | Chaves de API ausentes/inválidas ou cota esgotada | Verifique `API_KEY_GEMINI`/`API_KEY_GROQ` e os limites gratuitos de cada provedor |
| Mensagens muito longas cortadas | Limite de 2000 caracteres do Discord | O bot já divide automaticamente respostas longas em múltiplos blocos |
| Respostas lentas | Muitos usuários simultâneos ou latência da API de IA | Considere aumentar `COOLDOWN_SEGUNDOS` ou usar um modelo mais rápido |
| `ModuleNotFoundError` | Dependências não instaladas ou ambiente virtual não ativado | Rode `pip install -r requirements.txt` dentro do ambiente virtual correto |
| `/health` mostra um provedor como "indisponível" | Circuit breaker aberto após falhas consecutivas | Aguarde o tempo configurado em `CIRCUIT_BREAKER_TIMEOUT_SEGUNDOS`; o provedor volta a ser tentado automaticamente |
| Banco de dados não abre / erro de permissão em `dados/` | Pasta sem permissão de escrita | Garanta que o processo tem permissão de escrita na pasta do projeto (o SQLite e os backups ficam em `dados/`) |
| Mensagens de um usuário sendo ignoradas sem motivo aparente | Usuário pode estar na blacklist persistida no SQLite | Verifique a tabela `blacklist` no banco (`dados/desabafos.db`) |

---

## Considerações finais

Este bot **não substitui apoio profissional de saúde mental**. Em
casos de risco, ele já é instruído a indicar o **CVV (188)** e serviços
de emergência (192/190). Considere fixar essas informações também em
uma mensagem fixada no canal do servidor.
