# prospector

Sistema local de prospecção de clientes. Ele busca empresas no Google Maps, guarda tudo num SQLite, filtra e classifica cada lead, gera mensagens personalizadas, espera a sua aprovação e só então envia pelo WhatsApp, com ritmo humano e limites rígidos. Também recebe as respostas, respeita quem pede para parar e faz um único follow-up.

```
Google Maps ──► gosom/google-maps-scraper ──► CSV ──► SQLite (leads)
                                                        │ filtro: só celular BR, sem franquias
                                                        │ oferta: sem_site / so_social / tem_site
                                                        ▼
                         templates.yaml ──► fila do dia ──► VOCÊ APROVA ──► envio humanizado
                                                                              │ (WA-AKG)
                       painel (Flask) ◄── webhook de respostas ◄──────────────┘
```

Tudo roda no seu PC. O WA-AKG (com a sua sessão do WhatsApp) continua rodando à parte; o prospector só usa a API dele.

## Requisitos

- Linux com Docker e Docker Compose
- Python 3.12+ (para rodar a CLI fora do Docker, opcional)
- [WA-AKG](https://github.com/mrifqidaffaaditya/WA-AKG) rodando em `http://localhost:3000`, com uma sessão conectada e uma API key (Dashboard → Settings → API Keys)

## Instalação

```bash
git clone https://github.com/blackxzin/prospector.git
cd prospector
cp .env.example .env        # preencha WAAKG_SESSION, WAAKG_API_KEY e WEBHOOK_SECRET
```

Gere o segredo do webhook com `openssl rand -hex 32`.

### Opção A: tudo no Docker

```bash
docker compose up -d                 # sobe o scraper (porta 8080) e o painel (porta 5050)
alias prospector='docker compose run --rm prospector'
```

O container do prospector usa a rede do host, então enxerga o WA-AKG em `localhost:3000` e o scraper em `localhost:8080`. O `notify-send` funciona pelo socket D-Bus da sua sessão (montado no compose).

### Opção B: CLI local (venv) + scraper no Docker

```bash
docker compose up -d gmaps
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
source .venv/bin/activate
prospector painel &                  # painel + webhook em http://127.0.0.1:5050
```

## Uso do dia a dia

```bash
prospector buscar "pizzaria em Itaí SP"   # coleta (no mínimo uns 3 min), importa, filtra e classifica
prospector fila                           # gera a fila do dia e mostra cada lead com a mensagem final
prospector aprovar                        # interativo: [s]im / [n]ão / [e]ditar / [p]ular
prospector enviar --dry-run               # simula tudo (horários inclusive) sem enviar nada
prospector enviar                         # envia os aprovados, respeitando horário e limites
prospector status                         # funil, limites do dia, últimas respostas
```

Outros comandos:

| comando | o que faz |
|---|---|
| `prospector aprovar 3 5 7` / `--todos` | aprova por id ou todos os pendentes |
| `prospector rejeitar 4` | tira da fila (o lead fica como `rejeitado`) |
| `prospector enviar --aguardar` | fica rodando e espera as próximas janelas/dias |
| `prospector importar arquivo.csv --busca "..."` | importa um CSV já baixado do scraper |
| `prospector reclassificar` | reaplica `listas.yaml` depois de editar franquias/domínios |
| `prospector teste 14991234567` | manda UMA mensagem de exemplo para o seu próprio número |
| `prospector webhook http://localhost:5050/webhook` | cadastra o webhook no WA-AKG |
| `prospector retomar` | libera o envio depois de uma parada de emergência |
| `prospector desbloquear 14991234567` | tira um número da lista de bloqueio (correção manual) |
| `prospector painel` | sobe o painel web e o receptor de webhook |

### Painel

`http://127.0.0.1:5050` mostra:

- o funil: encontrados → aprovados → enviados → responderam → interessados;
- a lista de leads com filtro por busca, cidade e status, e troca manual de status;
- a fila de aprovação: dá para editar o texto, aprovar um a um ou todos de uma vez;
- o log de envios;
- exportação de CSV (respeita os filtros).

Aprovar no painel não envia nada. O envio só acontece com `prospector enviar`.

## Regras de envio

Todas ficam no `.env`. Os valores padrão:

| regra | padrão | variável |
|---|---|---|
| máximo por dia | 8 | `MAX_POR_DIA` |
| máximo por hora (janela móvel de 60 min) | 3 | `MAX_POR_HORA` |
| horário | seg–sex, 9h–11h30 e 14h–17h30 (America/Sao_Paulo) | `JANELAS`, `DIAS_UTEIS`, `TZ_PROSPECTOR` |
| intervalo aleatório entre envios | 4 a 12 min | `INTERVALO_MIN_SEG`, `INTERVALO_MAX_SEG` |
| "digitando..." antes de cada envio | 5 a 20 s, proporcional ao texto | `DIGITANDO_MIN_SEG`, `DIGITANDO_MAX_SEG` |
| follow-up | 1 lembrete, 3 dias úteis depois | `FOLLOWUP_DIAS_UTEIS` |

Garantias:

- **Nunca duas primeiras mensagens para o mesmo número**: checado antes de enfileirar, antes de enviar e por um índice único no banco (com e sem o nono dígito).
- **Parada de emergência**: qualquer erro do WA-AKG no envio (sessão caída, 401/403/404/429/503, "banned", "logged out" etc.) para tudo na hora, grava o motivo, dispara `notify-send` e bloqueia novos envios até você rodar `prospector retomar`. Um evento `connection.update` de desconexão vindo do webhook também para.
- **Antes de cada envio** o número é validado no WhatsApp (`/chat/{sessão}/check`), e a situação do lead é conferida de novo depois da espera: se ele respondeu ou pediu para parar enquanto o envio aguardava, a mensagem não sai.
- A saudação ("Bom dia"/"Boa tarde") é ajustada na hora do envio, porque a fila é gerada antes.
- **Log de tudo**: tabela `envios` com data, número, tipo, texto, status HTTP e resposta da API. Os envios de `--dry-run` também ficam lá, marcados.

## Respostas (webhook)

1. Rode o painel (`prospector painel` ou `docker compose up -d`).
2. Cadastre o webhook no WA-AKG: `prospector webhook http://localhost:5050/webhook`.
   Ele assina cada chamada com HMAC-SHA256 (`X-Webhook-Signature`) usando o `WEBHOOK_SECRET`, e o prospector recusa chamadas sem assinatura válida.

Quando um lead responde:

- o status vira `respondeu` (ou `interessado`, se a resposta parecer positiva), a resposta fica gravada e chega uma notificação no desktop;
- se a resposta tiver "não", "pare", "sair", "não tenho interesse", "me remove", "spam" ou algo parecido, o status vira `nao_quer` e o número entra na **lista de bloqueio permanente**. Nada mais é enviado para ele, nem se ele for encontrado de novo numa busca futura. A regra é conservadora de propósito: qualquer "não" conta. Se marcar alguém por engano, use `prospector desbloquear`;
- fora do horário comercial sai uma resposta automática curta (`fora_do_horario` no `templates.yaml`), **no máximo uma vez por contato**.

Só são considerados os números que já receberam uma mensagem real; conversas de outras pessoas são ignoradas.

## Follow-up

O `prospector fila` também cuida do follow-up:

- quem recebeu a primeira mensagem há 3 dias úteis e não respondeu ganha **um** lembrete na fila (que também passa pela sua aprovação);
- 3 dias úteis depois do lembrete, sem resposta, o lead vira `sem_resposta` e não recebe mais nada.

## Personalização

- **`templates.yaml`**: os textos. Cada oferta (`sem_site`, `so_social`, `tem_site`) tem pelo menos 3 variações, sorteadas por lead. Variáveis: `{nome}`, `{nota}`, `{avaliacoes}`, `{elogio}`, `{saudacao}`, `{vitrine}` (cardápio / planos / serviços, pela categoria), `{remetente}`. O arquivo é validado ao carregar: uma variável errada dá erro antes de qualquer envio.
- **`listas.yaml`**: franquias descartadas (Smart Fit, Skyfit, Bluefit, McDonald's, Subway…) e domínios que contam como "só rede social/guia" (Instagram, Facebook, Linktree, wa.me, iFood…). Depois de editar, rode `prospector reclassificar`.

## Classificação

| situação | resultado |
|---|---|
| telefone não é celular BR (11 dígitos com DDD e 9 depois do DDD) | descartado (`sem celular`) |
| nome bate com a lista de franquias | descartado (`franquia: X`) |
| sem site | oferta `sem_site`: site simples com cardápio/planos e botão de WhatsApp |
| site é Instagram, Facebook, Linktree, wa.me ou guia de terceiros | oferta `so_social`: mesma oferta de site |
| site próprio | oferta `tem_site`: automação de atendimento no WhatsApp |

Os celulares são guardados no formato `55DDXXXXXXXXX`. A deduplicação usa o telefone normalizado como chave, ou o link do Maps quando não há telefone.

## Status dos leads

`novo` → `na_fila` → `enviado` → `follow_up` → `sem_resposta`, e a qualquer momento `respondeu` / `interessado` / `nao_quer`. Também existem `descartado`, `rejeitado` (você recusou na fila), `sem_whatsapp` e `erro`.

## Testes

```bash
.venv/bin/pytest -q
```

Cobrem normalização de telefone, classificação de oferta e franquias, deduplicação, janela de horário (com fuso), limites por dia e por hora, intervalo, dias úteis, geração de mensagens, fila e follow-up, envio com um WA-AKG falso (dry-run, parada de emergência, nada de primeira mensagem duplicada), webhook (HMAC, opt-out, resposta fora do horário) e o painel (escape de HTML, CSV seguro, proteção contra POST de outra origem).

## Estrutura

```
prospector/
  cli.py          comandos
  app.py          liga config, banco, templates e clientes
  config.py       .env e validação
  db.py           esquema SQLite e consultas
  phone.py        normalização de telefone
  classify.py     franquias e tipo de oferta
  importer.py     CSV do scraper -> leads
  scraper.py      API do google-maps-scraper
  messages.py     templates.yaml -> mensagens
  fila.py         fila do dia, aprovação, follow-up
  schedule.py     janelas, limites, intervalos, dias úteis
  sender.py       envio humanizado e parada de emergência
  whatsapp.py     API do WA-AKG
  replies.py      webhook: opt-out, notificação, resposta automática
  web.py          painel Flask + endpoint /webhook
templates.yaml    textos das mensagens
listas.yaml       franquias e domínios de redes sociais
```

## Avisos

- Use com responsabilidade e respeite a LGPD: contate só empresas, com mensagens relevantes, e honre na hora qualquer pedido de parada (o sistema faz isso por você).
- Mensagens não solicitadas em volume podem levar o WhatsApp a restringir o número. Os limites padrão são baixos de propósito; aumente por sua conta e risco.
- O `.env` contém a API key do WA-AKG e não vai para o git (`.gitignore`).

## Licença

MIT
