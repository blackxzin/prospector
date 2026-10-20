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

## Primeiro dia: passo a passo

1. **Confira o que falta:** `prospector checar`. Ele testa o `.env`, o scraper, o WA-AKG (sessão conectada), o webhook, o Pix, o backup e se há leads, e diz como resolver cada item.
2. **Suba o WA-AKG** com a sua sessão conectada e preencha no `.env`: `WAAKG_URL` (se não for `http://localhost:3000`), `WAAKG_SESSION` e `WAAKG_API_KEY`.
3. **Cadastre o webhook** (para receber as respostas): `prospector webhook http://localhost:5050/webhook`.
4. **Teste no seu número:** `prospector teste 14999999999`.
5. **Abra o painel:** `prospector painel` e depois `http://127.0.0.1:5050` (ou instale como serviço, veja abaixo).
6. **Encha o estoque de leads:** em **Buscas**, ajuste nichos e cidades (o botão "Cidades por raio" puxa todas as cidades num raio a partir de Itaí) e clique em **Rodar buscas**.
7. **Gere a fila:** em **Fila e envio** (ou na Visão geral), clique em **Gerar fila do dia**, revise cada mensagem e aprove.
8. **Envie:** clique em **Enviar aprovados**. O envio respeita horário, limites, intervalo e "digitando", e pode ser pausado a qualquer momento. Com "esperar próximas janelas" marcado, ele continua sozinho no próximo horário útil.
9. **Acompanhe:** as respostas chegam como notificação; a Visão geral mostra o funil, as respostas por dia, cidade, nicho e horário.

### Deixar rodando sozinho (opcional)

- **Painel sempre ligado:** `scripts/prospector-painel.service` é um serviço de usuário do systemd (instruções dentro do arquivo).
- **Rotina diária:** `scripts/crontab.exemplo` faz backup às 23h, buscas de madrugada, gera a fila às 8h e manda os alertas às 8h30. Você continua aprovando tudo antes do envio.

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
| `prospector financeiro [--notificar]` | resumo do mês, alertas e sugestões do gerenciador |
| `prospector checar` | confere se está tudo pronto para rodar e diz como resolver o que falta |
| `prospector regiao "Itaí" --uf SP --raio 60 [--salvar]` | cidades num raio, para as buscas |
| `prospector lote [--listar] [--max N]` | buscas em lote de `buscas.yaml` |
| `prospector checar-sites` | testa os sites dos leads (oferta `site_problema`) |
| `prospector checar-presenca [--limite N]` | busca Instagram/site/cardápio online dos leads novos no DuckDuckGo |
| `prospector ab` | resultado do teste A/B dos textos |
| `prospector proposta <projeto>` | proposta em PDF |
| `prospector relatorio [--dias 7] [--notificar]` | relatório do período |
| `prospector backup [--listar]` | backup do banco |
| `prospector painel` | sobe o painel web e o receptor de webhook |

### Painel

`http://127.0.0.1:5050` (tema claro/escuro automático, funciona no celular):

- **Visão geral**: faixa "Agora" com o status ao vivo (dentro/fora do horário, envios do dia num anel, limite da hora, próxima janela) e os botões do dia (rodar buscas, gerar fila, enviar/pausar); avisos do que falta configurar; envios × respostas por dia (30 dias), funil completo, mapa de calor de quando os clientes respondem (dia × hora), taxa de resposta por cidade e por nicho, leads por cidade, alertas, sugestões e o teste A/B dos textos;
- **Buscas**: nichos × cidades, cidades por raio, progresso, botão para rodar (em segundo plano, com pausa);
- **Fila e envio**: revisar, editar e aprovar mensagens; barra com aprovadas, envios de hoje e da última hora, e o botão Enviar/Pausar com o registro ao vivo do que está acontecendo;
- **Leads**: filtro por busca, cidade e status, troca manual de status, botão "Fechou" e exportação de CSV (respeita os filtros);
- **Fila de aprovação**: dá para editar o texto, aprovar um a um ou todos de uma vez;
- **Projetos**: possíveis fechamentos, projetos com recebido, a receber, despesas e lucro;
- **Financeiro**: contas a receber, despesas, mês a mês;
- **Log de envios**.

O painel não usa JavaScript nem nada de fora (gráficos em SVG gerados no servidor), e manda cabeçalhos de segurança (CSP, sem iframe).

Aprovar no painel não envia nada. O envio só acontece com `prospector enviar`.

## Projetos, lucro e gerenciador

Quando um lead fecha, o prospector cuida do dinheiro.

**Detecção de fechamento.** Se a resposta no WhatsApp tiver algo como "fechado", "pode fazer", "qual o pix?", "como eu pago?" ou "vou querer", o lead vira `interessado` com o aviso **possível fechamento**, e chega uma notificação. Nada é registrado sozinho: você confirma no painel (Projetos, ou o botão "Fechou" na lista de leads).

**Projeto.** Ao confirmar, o formulário já vem preenchido com o preço padrão do serviço (`financeiro.yaml`): site para `sem_site`/`so_social`, automação para `tem_site`. Ao salvar:

- as parcelas são criadas: entrada (padrão 50%) no dia do fechamento e o restante no prazo de entrega;
- o lead vira `cliente` (e um "não" dele daqui em diante é conversa normal, não pedido de descadastro);
- com mensalidade, a cobrança de cada mês aparece sozinha a partir de um mês depois da entrega;
- cancelar o projeto apaga as parcelas ainda não pagas.

Na página do projeto você marca parcelas como recebidas ("Recebi"), lança despesas (domínio, hospedagem…) e vê recebido, a receber e lucro. Valores são guardados em centavos, sem erro de arredondamento.

**Gerenciador inteligente** (Visão geral, ou `prospector financeiro`):

- *O que fazer agora*: parcelas vencidas para cobrar, pagamentos que vencem em até 3 dias, prazos estourados ou próximos, projetos entregues e não quitados, possíveis fechamentos, interessados sem retorno e mensagens esperando aprovação;
- *Sugestões*: quanto falta para a meta do mês (e quantos projetos isso representa), qual busca/oferta converte melhor e qual responde pouco, aviso de preço baixo quando a taxa de fechamento passa de 30%, clientes sem mensalidade, margem baixa, estoque de leads acabando;
- *Números*: lucro do mês, meta, a receber, previsto para 30 dias (parcelas + mensalidades), receita recorrente, ticket médio, gráfico receita × despesas dos últimos 6 meses, funil completo até "fecharam" e uma tabela de conversão por busca.

As regras são explícitas e estão em `prospector/insights.py`: dá para ver de onde sai cada número. Para receber os alertas importantes no desktop todo dia, coloque no cron:

```
0 9 * * 1-5  cd ~/prospector && .venv/bin/prospector financeiro --notificar
```

Edite o `financeiro.yaml` com os seus preços, custos, prazo, entrada e meta mensal. Os valores que vêm nele são só exemplos.

## Extras

### Pix e cobrança automática

Com `PIX_CHAVE`, `PIX_NOME` e `PIX_CIDADE` no `.env`, cada parcela em aberto mostra no painel o **QR Code e o Pix copia e cola** com o valor certo. O código segue o padrão BR Code do Banco Central e é gerado no seu PC, sem banco nem API no meio.

Parcela vencida de um cliente vira uma mensagem de **cobrança educada** na fila (com o Pix), também esperando sua aprovação: no máximo 2 lembretes por parcela, com 5 dias entre eles. Se a parcela for marcada como paga antes do envio, a cobrança é cancelada sozinha. Cobranças não contam no limite diário de prospecção, mas respeitam horário, intervalo e "digitando".

### Teste A/B dos textos

Cada variação da primeira mensagem (`templates.yaml`) tem um identificador. O sistema mede quantos responderam bem a cada uma ("não quero" conta como resposta negativa) e passa a sortear mais as que funcionam (amostragem de Thompson), sem abandonar as outras. Veja em `prospector ab` ou no card "Textos que mais funcionam". Se você editar um texto, ele vira uma variação nova e começa do zero.

### Checagem de sites (oferta `site_problema`)

Depois de cada busca, os sites dos leads com site próprio são testados: fora do ar, sem HTTPS ("não seguro"), lento (mais de 5 s) ou sem versão para celular. Com problema, a oferta vira `site_problema` e a mensagem cita o problema ("Fui abrir o site de vocês e reparei que o site está fora do ar…"). Para rodar na mão: `prospector checar-sites`. Por segurança, só acessa endereços públicos (nada de rede local), com timeout e limite de tamanho.

**Sinais do site (para personalizar).** Na mesma leitura da página inicial, o prospector anota fatos objetivos em `site_sinais`: `sem_whatsapp` (nenhuma menção a WhatsApp, nem em script de widget), `sem_telefone` (nenhum link `tel:`), `sem_descricao` (sem meta description para o Google) e `rodape_AAAA` (© com 3 anos ou mais). Página minúscula ou só JavaScript não é julgada. Quem tem site mas `sem_whatsapp` recebe textos próprios (`primeira.tem_site_sem_whatsapp` no `templates.yaml`: "não achei um botão de WhatsApp no site de vocês"); os outros sinais ficam só como informação. Só vale no Brasil: os textos e diagnósticos são em português, e de fora muito site "cai" por bloqueio. Leads já checados antes ganham os sinais na próxima rodada de `checar-sites`.

**Ameaça, denúncia ou golpe.** Resposta falando de advogado, Procon, denúncia, assédio, golpe, GDPR/RGPD/DSGVO, polícia etc. (pt, en, es, fr, de, it) vira alerta urgente "NÃO RESPONDA" no computador e no gerenciador por 14 dias, marca `escalar` no lead e nunca recebe resposta automática. Não bloqueia sozinha, porque "meu advogado precisa de um site" é cliente; se a mesma mensagem pedir para parar, o opt-out vale normalmente.

Antes de um lead entrar na fila da 1ª mensagem, o prospector busca "nome + cidade" no DuckDuckGo (o Google Maps muitas vezes não traz o site nem o Instagram). Achou Instagram/Facebook com o nome do negócio no endereço: oferta `so_social` e o link vai para `site`. Achou site próprio (nome no domínio): `tem_site`. Achou cardápio online com robô de WhatsApp (anota.ai, whatsmenu, goomer, cardapioweb, iFood) com o nome e a cidade batendo: lead descartado. Se só o nome bate, nada muda e fica uma observação no lead (aparece na fila). Guias/diretórios (restaurantguru, tripadvisor...) são ignorados. As listas ficam em `listas.yaml` (`dominios_robo`, `dominios_diretorio`). Cada lead é buscado uma vez só (`presenca_checada_em`); se o DuckDuckGo bloquear, o lead segue como está e a busca fica para a próxima. Desligue com `CHECAR_PRESENCA=0` no `.env`; para rodar na mão: `prospector checar-presenca`.

### Buscas em várias cidades

`prospector regiao "Itaí" --uf SP --raio 60` lista todas as cidades até 60 km (base do IBGE com coordenadas e DDD; baixada uma vez e guardada em `data/municipios.csv`). Com `--salvar`, elas vão para o `buscas.yaml`. O `buscas.yaml` já vem com as 21 cidades num raio de 60 km de Itaí e 14 nichos (294 buscas).

Tudo isso também está no painel, em **Buscas**: editar nichos e cidades, ver as cidades por raio antes de salvar (adicionar ou substituir), acompanhar o progresso, ver as próximas buscas e o resultado das últimas.

### Fora do Brasil (Europa, EUA, Canadá)

O `mundo.yaml` traz países, cidades (com fuso) e nichos por idioma. Cada lead guarda **país, idioma e fuso**.

```bash
prospector lote --mundo --listar                # buscas pendentes pelo mundo
prospector lote --mundo --pais ES,PT,IT --max 6 # só estes países
prospector buscar "barber shop in Austin, TX, United States" --pais US --fuso America/Chicago --cidade Austin
prospector fila --pais ES,PT --tamanho 8        # a fila é só do Brasil por padrão; 'todos' mistura tudo
```

- **Telefone:** qualquer país (biblioteca `phonenumbers`). Só celular entra; fixo é descartado. EUA e Canadá não
  distinguem fixo de celular, então a confirmação vem da checagem no WA-AKG (a maioria não tem WhatsApp lá).
- **Textos:** `templates.en.yaml`, `.es`, `.fr`, `.de`, `.it` e `.pt-PT` sobrescrevem o `templates.yaml`. Toda abordagem
  traz a linha de saída (STOP/PARAR); sem link do portfólio (ele está em português) e sem preço em reais.
- **Horário:** o horário comercial (dias úteis, janelas) é medido no relógio do lead. Os limites por intervalo,
  hora e dia continuam globais. Lead de outro fuso não trava os do Brasil: sai primeiro quem pode sair antes.
- **Respostas:** opt-out, interesse e resposta automática do negócio são reconhecidos em pt, en, es, fr, de e it.
- **Atenção legal:** mensagem comercial fria tem regras próprias (CASL no Canadá, TCPA nos EUA, GDPR/ePrivacy na UE e
  o Reino Unido) e o WhatsApp proíbe envio em massa sem consentimento. Confirme o que vale para o seu caso antes de
  enviar em escala, e respeite todo STOP na hora.

### Buscas em lote

Liste nichos e cidades no `buscas.yaml` e rode `prospector lote`: cada combinação nicho × cidade é buscada uma vez só (as já feitas são puladas, inclusive as manuais antigas), até `max_por_execucao` por vez. Uma busca com erro não para as outras e é tentada de novo depois. `prospector lote --listar` mostra o que falta.

### Proposta em PDF

Na página do projeto, o botão "Proposta (PDF)" gera uma proposta com o serviço, o que está incluído (lista `inclui` no `financeiro.yaml`), o valor, as condições (entrada e restante), o prazo, a mensalidade e, se o Pix estiver configurado, o QR Code da entrada. Pela CLI: `prospector proposta <id-do-projeto>`.

### Relatório semanal

`prospector relatorio` resume os últimos 7 dias (leads novos, enviados, respostas, interessados, fechamentos, recebido, despesas, lucro e a melhor busca) e salva em `data/relatorios/`. Com `--notificar`, manda o resumo para o desktop.

### Backup

`prospector backup` copia o banco (com a API de backup do SQLite, seguro mesmo com o painel aberto), confere a integridade, guarda junto os arquivos `.yaml` e mantém os últimos 30 em `~/prospector-backups` (`BACKUP_DIR`, `BACKUP_MANTER`). O `.env` não vai no backup, porque tem a sua API key. O painel avisa se o último backup tiver mais de 2 dias.

### Cron sugerido

```
# backup todo dia às 23h
0 23 * * *    cd ~/prospector && .venv/bin/prospector backup
# buscas em lote de madrugada
0 3 * * *     cd ~/prospector && .venv/bin/prospector lote
# alertas do gerenciador às 9h (dias úteis)
0 9 * * 1-5   cd ~/prospector && .venv/bin/prospector financeiro --notificar
# relatório da semana na segunda às 8h
0 8 * * 1     cd ~/prospector && .venv/bin/prospector relatorio --notificar
```

Para o `notify-send` funcionar no cron, acrescente `DISPLAY=:0 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus` antes do comando.

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
  o exemplo de site para quem pediu você faz por conta própria e manda pela conversa;
- se a resposta tiver "não", "pare", "sair", "não tenho interesse", "me remove", "spam" ou algo parecido, o status vira `nao_quer` e o número entra na **lista de bloqueio permanente**. Nada mais é enviado para ele, nem se ele for encontrado de novo numa busca futura. A regra é conservadora de propósito: qualquer "não" conta. Se marcar alguém por engano, use `prospector desbloquear`;
- fora do horário comercial sai uma resposta automática curta (`fora_do_horario` no `templates.yaml`), **no máximo uma vez por contato**.

Só são considerados os números que já receberam uma mensagem real; conversas de outras pessoas são ignoradas.

## Follow-up

O `prospector fila` também cuida do follow-up:

- quem recebeu a primeira mensagem há 3 dias úteis e não respondeu ganha **um** lembrete na fila (que também passa pela sua aprovação);
- 3 dias úteis depois do lembrete, sem resposta, o lead vira `sem_resposta` e não recebe mais nada.

## Personalização

- **`templates.yaml`**: os textos. Cada oferta (`sem_site`, `so_social`, `tem_site`) tem pelo menos 3 variações, sorteadas por lead. Variáveis: `{nome}`, `{nota}`, `{avaliacoes}`, `{elogio}`, `{saudacao}`, `{vitrine}` (cardápio / planos / serviços, pela categoria), `{remetente}`, `{apresentacao}`, `{portfolio}`. O arquivo é validado ao carregar: uma variável errada dá erro antes de qualquer envio.
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

`novo` → `na_fila` → `enviado` → `follow_up` → `sem_resposta`, e a qualquer momento `respondeu` / `interessado` / `nao_quer`; quem fecha vira `cliente`. Também existem `descartado`, `rejeitado` (você recusou na fila), `sem_whatsapp` e `erro`.

## Testes

```bash
.venv/bin/pytest -q
```

Cobrem normalização de telefone, classificação de oferta e franquias, deduplicação, janela de horário (com fuso), limites por dia e por hora, intervalo, dias úteis, geração de mensagens, fila e follow-up, envio com um WA-AKG falso (dry-run, parada de emergência, nada de primeira mensagem duplicada), webhook (HMAC, opt-out, resposta fora do horário, detecção de fechamento), financeiro (dinheiro em centavos, parcelas, mensalidades, cancelamento, lucro), gerenciador (alertas e sugestões) e o painel (escape de HTML, CSV seguro, cabeçalhos de segurança, proteção contra POST de outra origem).

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
  web_finance.py  painel: visão geral, projetos, financeiro
  finance.py      projetos, parcelas, despesas, mensalidades (centavos)
  insights.py     gerenciador: alertas, sugestões, previsão
  charts.py       gráficos SVG
  backup.py       backup do SQLite com rotação
  pix.py          Pix copia e cola (BR Code) e QR Code
  sitecheck.py    checagem de sites (com proteção contra acesso à rede local)
  lote.py         buscas em lote
  regiao.py       cidades num raio (base do IBGE)
  jobs.py         tarefas em segundo plano do painel (buscas e envio, com pausa)
  checagem.py     "está tudo pronto para rodar?"
  metrics.py      métricas por dia, cidade, nicho e horário
  web_ops.py      painel: visão geral, buscas, tarefas
  pdf.py          gerador de PDF mínimo     proposta.py  proposta comercial
  relatorio.py    relatório semanal
  templates/      HTML do painel     static/  CSS
templates.yaml    textos das mensagens
listas.yaml       franquias e domínios de redes sociais
financeiro.yaml   preços, custos, entrada, meta mensal, itens incluídos
buscas.yaml       nichos × cidades das buscas em lote
```

## Avisos

- Use com responsabilidade e respeite a LGPD: contate só empresas, com mensagens relevantes, e honre na hora qualquer pedido de parada (o sistema faz isso por você).
- Mensagens não solicitadas em volume podem levar o WhatsApp a restringir o número. Os limites padrão são baixos de propósito; aumente por sua conta e risco.
- O `.env` contém a API key do WA-AKG e não vai para o git (`.gitignore`).

## Licença

MIT
