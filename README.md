# Monitor de Mangás

App web para rodar no seu servidor caseiro: você cadastra as páginas dos
mangás que acompanha e ele te avisa no Discord quando detectar capítulo
novo.

## Rodando com Docker (recomendado)

Pré-requisito: Docker e Docker Compose instalados no servidor.

```bash
docker compose up -d --build
```

Acesse: `http://IP_DO_SERVIDOR:5050`

Os dados (contas, mangás, configurações) ficam salvos no volume
`manga-data`, sobrevivem a reinícios e atualizações do container.

**Nota sobre o build:** a imagem inclui um navegador Chromium (usado pra
sites com JavaScript — veja mais abaixo), então fica bem maior que um app
Flask comum (~300-400MB a mais) e a primeira vez que você rodar
`docker compose up -d --build` vai demorar mais, porque ele baixa e
instala o navegador.

## Rodando sem Docker (Python direto)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Acesse: `http://localhost:5050`
Para rodar em segundo plano de forma permanente, crie um serviço `systemd`
ou use `tmux`/`screen`/`pm2`.

## Porta usada

O app sobe por padrão na porta **5050**. Dá pra trocar definindo a variável
de ambiente `PORT` e ajustando o mapeamento no `docker-compose.yml`
(ex: `"5051:5051"` + `PORT=5051`), caso essa porta já esteja em uso em algum
outro serviço do servidor.

## Banco de dados: SQLite (padrão) ou Postgres

Por padrão o app usa SQLite (arquivo salvo no volume `/data`), não precisa
configurar nada. Se preferir usar um Postgres que já existe no servidor,
defina a variável de ambiente `DATABASE_URL` no `docker-compose.yml`:

```yaml
environment:
  - DATABASE_URL=postgresql://usuario:senha@HOST:5432/manga_monitor
```

- Peça pro dono do servidor a string de conexão (usuário, senha, host, porta,
  nome do banco). Se o Postgres estiver rodando no host (fora de um
  container), use `host.docker.internal` no lugar de `HOST` (ou o IP do
  host na rede docker, dependendo de como está configurado).
- Crie o banco `manga_monitor` antes de subir o app (o app cria as tabelas
  sozinho na primeira execução, mas o banco em si precisa já existir).
- Quando `DATABASE_URL` está definida, ela tem prioridade e o SQLite não é
  usado.

## Contas de usuário

O app tem login. Cada pessoa cria sua própria conta (usuário e senha) e só
vê e gerencia os mangás/fontes que ela mesma cadastrou — inclusive o
webhook do Discord é configurado por conta, então cada um recebe aviso no
canal que quiser.

- Acesse `/register` pra criar a primeira conta.
- Depois é só entrar por `/login`.

**Chave secreta (SECRET_KEY):** se a variável `SECRET_KEY` não estiver
definida (ou ainda tiver o valor de exemplo `troque-esta-chave-em-producao`),
o app gera uma chave aleatória sozinho e guarda em `/data/secret_key`, pra
ela sobreviver a reinícios. Com o valor de exemplo qualquer pessoa que
conhecesse a chave conseguiria forjar um login. Se quiser controlar a
chave você mesmo, defina `SECRET_KEY` com um texto longo e aleatório no
Portainer/compose.

**Aviso importante:** essa autenticação é simples (usuário/senha com hash,
sessão via cookie) — boa o suficiente pra uso pessoal/doméstico, mas **não**
tem proteção contra ataques de força bruta, recuperação de senha, nem CSRF
token. Se for expor esse app pra fora da sua rede local (internet), o ideal
é colocar atrás de HTTPS (reverse proxy tipo Nginx/Caddy com certificado) ou
acessar só via VPN, em vez de abrir a porta direto pra internet.

## Interface mais compacta

Pra não deixar a tela gigante quando você tem vários mangás/fontes
cadastrados, cada grupo agora tem duas camadas:

- **Sempre visível:** nome do mangá, quantas fontes tem, e selos de resumo
  (último capítulo notificado, se tem webhook próprio, se alguma fonte
  está com problema) — e logo abaixo, **"+ Adicionar fonte a este
  grupo"**, que abre com um clique só (sem precisar abrir mais nada) e
  pede só **nome da fonte e link**. O intervalo de checagem fica no padrão
  de 5h; se quiser mudar isso ou configurar seletor/regex/horário fixo,
  edite a fonte depois de criada.
- **Atrás de um segundo clique ("Ver fontes e configurações"):** a tabela
  completa (intervalo, última checagem, capítulo detectado, status, ações)
  e o campo de webhook do grupo.

Isso deixa o fluxo mais comum — "quero adicionar mais um site pra um mangá
que já acompanho" — em um clique e dois campos, sem precisar abrir a
tabela toda pra chegar lá.

O formulário completo do topo ("+ Cadastrar mangá novo") segue fechado por
padrão, só necessário pra mangá que ainda não tem grupo.

## Adicionar fonte mais rápido a um mangá já cadastrado

Se o mangá já tem grupo criado, não precisa usar o formulário completo do
topo de novo. Dentro da seção de cada grupo (mais abaixo na tela inicial)
tem um formulário compacto — só **nome da fonte** e **link** — que já usa
o nome do grupo e o webhook daquele grupo automaticamente, sem precisar
redigitar ou colar de novo. O intervalo padrão vem preenchido com 5h, mas
dá pra mudar ali mesmo.

Pra configurações mais específicas daquela fonte (seletor CSS, regex,
horário fixo, forçar navegador), cadastre por esse formulário rápido e
depois clique em "Editar" na fonte pra ajustar os detalhes — ou use o
formulário completo do topo desde o início.

No formulário completo do topo, o campo **Grupo** agora sugere os grupos
que você já tem (autocomplete) assim que você começa a digitar, pra evitar
erro de digitação criando um grupo novo por engano.

## Como cadastrar um mangá

Depois de entrar na conta, na página inicial tem o formulário **"Adicionar
fonte de mangá"** com estes campos:

- **Nome da fonte** (obrigatório): identifica essa fonte específica, ex:
  "One Piece (SiteA)".
- **Grupo**: o nome do mangá em si. Deixe em branco pra usar o próprio nome
  da fonte, ou preencha com o mesmo valor em várias fontes pra agrupar (veja
  a seção "Várias fontes pro mesmo mangá" abaixo).
- **URL da página de capítulos** (obrigatório): o link da página que lista
  os capítulos.
- **Seletor CSS** (opcional): restringe a checagem a um trecho da página
  (ex: só a lista de capítulos), evitando avisos falsos por causa de
  banners, contador de visitas etc. Exemplos comuns: `.chapter-list`,
  `#chapters ul`, `.eplister`. Pra achar o seletor certo, clique com o
  botão direito no elemento da lista de capítulos no navegador →
  Inspecionar.
- **Regex do número do capítulo** (opcional): sobrescreve o padrão usado
  pra identificar o número do capítulo no texto da página (veja a seção de
  dedup abaixo).
- **Forçar uso de navegador** (checkbox avançado): normalmente não precisa
  mexer — veja a seção "Sites com JavaScript".
- **Checar a cada (horas)** OU **horário fixo diário**: escolha uma das
  duas. Preenchendo "Checar a cada", o app conta a partir da última
  checagem (padrão: 5h). Preenchendo o horário fixo (ex: `17:00`), ele
  ignora o campo de intervalo e passa a checar essa fonte uma vez por dia,
  sempre nesse horário (com margem de até 15 min).
- **Webhook do Discord deste grupo** (opcional): veja a seção de
  notificações abaixo.

Depois de cadastrado, o app checa automaticamente respeitando a
configuração da fonte. Também dá pra forçar uma checagem manual clicando em
"Checar agora", e editar qualquer campo depois clicando em "Editar" — sem
precisar apagar e recadastrar (o que faria perder o histórico e o progresso
daquele grupo).

## Várias fontes pro mesmo mangá (sem notificação duplicada)

Se você acompanha o mesmo mangá em sites diferentes (porque um pode
atualizar antes do outro), cadastre uma fonte pra cada site e coloque o
**mesmo nome no campo "Grupo"** em todas — ex: `One Piece` nas duas.

Como funciona o dedup:

- O app tenta extrair automaticamente o número do capítulo do texto da
  página (reconhece padrões como "Capítulo 1120", "Chapter 1120",
  "Cap. 1120", "Episódio 45"). Se o site tiver um padrão diferente, dá pra
  sobrescrever isso no campo **Regex do número do capítulo** daquela fonte.
- Quando o número do capítulo é identificado, a comparação é exata: o
  grupo guarda "o último capítulo notificado foi o X". Assim que qualquer
  fonte do grupo detectar um capítulo com número maior que X, ela notifica
  e atualiza esse valor — as outras fontes do mesmo grupo não avisam de
  novo sobre esse mesmo capítulo, só quando surgir um número ainda maior.
- Se o app não conseguir identificar o número do capítulo em nenhuma fonte
  (site sem numeração clara no texto), ele cai num modo de reserva: detecta
  mudança de conteúdo comum, mas respeita um **cooldown por grupo**
  (configurável em Configurações, padrão 2h) pra evitar que duas fontes do
  mesmo grupo notifiquem quase ao mesmo tempo pelo mesmo evento. Esse modo
  é menos preciso que a comparação por número — se puder, ajuste o seletor
  CSS/regex pra garantir que o número seja capturado.

## Detecção automática de seletor (sem precisar configurar nada)

Além de detectar sozinho quando precisa de navegador, o app também tenta
achar sozinho **qual link da página é o capítulo mais recente**, sem você
precisar inspecionar o HTML e preencher o campo "Seletor CSS" manualmente:

- Ele varre todos os links (`<a>`) da página procurando quais têm texto que
  bate com o padrão de capítulo (ex: "Cap. 1190", "Capítulo 1190"), e usa só
  esses links pra checagem — ignorando o resto da página (comentários,
  curtidas, contador de visitas), mesmo que essas partes também mencionem
  números.
- Se além disso a página não retornar **nenhum** número de capítulo em
  lugar nenhum (nem nos links, nem no resto do texto), isso é tratado como
  sinal forte de bloqueio/verificação anti-bot ou página que não carregou
  direito — e o app já cai automaticamente pro navegador headless pra
  tentar de novo, mesmo que o texto recebido pareça "normal" em tamanho.

Isso cobre a maioria dos sites automaticamente. O campo **"Seletor CSS"**
continua existindo pra quando você quiser mais precisão manual, ou pra
sites onde a detecção automática não acerte de primeira — nesse caso ele
tem prioridade sobre a detecção automática.

## Como o app acha o capítulo sozinho (várias estratégias)

Pra cada fonte o app tenta, em ordem, da pista mais confiável pra menos
confiável, e para na primeira que der resultado:

1. **Seletor CSS manual**, se você preencheu (se ele não achar nada, o app
   não desiste: segue pras próximas).
2. **Texto dos links** parecido com "Cap. 45", "Capítulo 45", "Chapter 45".
3. **Abrir a aba "Capítulos"** (modo navegador): se a lista não está na
   página, clica sozinho em abas/botões com nomes tipo "Capítulos",
   "Capítulos (120)", "Chapters", "Episódios".
4. **Rolar a página**, pra listas que só carregam quando você desce.
5. **Endereço dos links**: `/capitulo-45`, `/chapter/45`, ou vários links
   numerados dentro da própria página do mangá (`.../one-piece/1191`,
   `.../one-piece/1190`...). Só conta link que pertence ao mangá, pra não
   confundir com a barra lateral de "últimas atualizações" de outros
   mangás, e paginação (`/page/2`) é ignorada.
6. **Dados JSON**: lista de capítulos embutida na página (`__NEXT_DATA__`
   e parecidos) ou carregada em segundo plano pelo site (XHR/fetch),
   que o navegador do app observa durante a carga.

As estratégias 5 e 6 só valem com o regex padrão: se você definiu um regex
próprio numa fonte, o app respeita e não adivinha nada além disso.

**Diagnóstico automático:** se nenhuma estratégia achar o capítulo, o app
guarda um resumo do que viu (abas e botões, links com número, requisições
JSON, se tentou clicar numa aba). Ele aparece na tela **Editar** da fonte,
em "Último diagnóstico", e também ao passar o mouse no traço (—) da coluna
"Cap. detectado". É só copiar e mandar pra quem estiver ajudando, sem
precisar abrir o Inspecionar.

## Sites com JavaScript ou bloqueio de bot — detecção automática de navegador

Alguns sites só mostram a lista de capítulos depois de rodar JavaScript no
navegador (MangaPlus é um exemplo comum) — uma requisição HTTP simples não
retorna esse conteúdo. **Isso já é resolvido automaticamente**, não precisa
saber disso na hora de cadastrar:

1. O app tenta primeiro o jeito simples (requisição HTTP direta, mais
   rápido e mais leve).
2. Se o conteúdo voltar muito curto, vazio, com sinais de "ative o
   JavaScript", **ou se não achar nenhum número de capítulo em lugar
   nenhum da página** (sinal de bloqueio anti-bot, por exemplo), ele refaz
   a checagem automaticamente usando um navegador Chromium headless (via
   Playwright), que executa o JavaScript da página antes de extrair o
   conteúdo — e costuma passar por esse tipo de verificação melhor que uma
   requisição simples.
3. Assim que detecta que uma fonte precisa do navegador, ele **memoriza**
   isso (aparece o selo "JS detectado" na lista) e vai direto pro modo
   navegador nas próximas checagens dessa fonte.

O navegador headless só roda durante os poucos segundos de cada checagem —
não fica em segundo plano consumindo recursos o resto do tempo, e só é
usado pelas fontes que realmente precisam (as demais continuam no modo
simples, mais leve).

Se por algum motivo a detecção automática errar pra um site específico, o
checkbox avançado **"Forçar uso de navegador"** (no cadastro ou na edição
da fonte) pula a detecção e sempre usa o navegador headless pra aquela
fonte.

## Sites com proteção anti-bot mais forte

Alguns sites usam proteção que detecta especificamente quando é um
navegador automatizado (mesmo com Playwright/Chromium headless) — não só
"tem JavaScript ou não". Pra aumentar a chance de passar por isso, o
app usa o **patchright** (uma versão do Playwright com várias correções
nos detalhes que costumam entregar que é automação) em vez do Playwright
puro, além de:

- Esconder sinais adicionais de automação (`navigator.webdriver`,
  plugins vazios, etc.)
- Usar um idioma e tamanho de tela mais realistas
- Tentar esperar a rede ficar quieta, mas se a página nunca "sossega"
  (sites com comentários/analytics que ficam fazendo requisições sem
  parar), tenta de novo só esperando o HTML carregar, com uma pausa extra

Isso ajuda bastante, mas alguns sites com proteção muito agressiva
(Cloudflare em modo mais estrito, por exemplo) podem continuar
bloqueando mesmo assim — não é algo que dá pra garantir 100% de contorno.
A instalação do navegador do patchright no build é **opcional** — se
falhar por qualquer motivo (ou se o patchright não estiver disponível em
tempo de execução), o app detecta isso automaticamente e usa o
Playwright normal no lugar, sem travar nem quebrar nada.

**Nota sobre o build:** quando a instalação do patchright dá certo, a
imagem baixa um segundo navegador Chromium (além do que o Playwright já
baixava), deixando o build ainda mais demorado na primeira vez — é o
preço de ter mais chance de passar por proteções mais chatas.

**Espera extra pra telas de verificação:** alguns sites mostram uma tela
tipo "Um momento... verificando seu navegador" por alguns segundos antes
de liberar o conteúdo de verdade (comum em proteções tipo Cloudflare). O
app detecta esse tipo de tela e espera um pouco (até 10 segundos no
total, em 2 tentativas de 5s), em vez de desistir na hora que vê essa
tela. Esperar mais que isso só segura o navegador à toa: como só um
navegador roda por vez, uma fonte travada atrasaria todas as outras
checagens que precisam dele. Proteções mais sofisticadas (que analisam
comportamento do navegador, não só tempo de espera) podem continuar
bloqueando — não é algo que dá pra garantir 100% de contorno.

**Lista de capítulos atrás de uma aba:** alguns sites só mostram os
capítulos depois de clicar numa aba ou botão (ex: "Capítulos"). No modo
navegador, se a lista não estiver na página, o app procura sozinho abas e
botões com textos como "Capítulos", "Capítulos (120)", "Lista de
capítulos", "Chapters" ou "Episódios", clica (no máximo 2 de cada tipo, em
até 8 segundos) e só então lê a lista. Se a lista já está visível, ele não
clica em nada. Abas com só um ícone ou com nome fora do comum não são
reconhecidas — nesses casos o seletor CSS manual continua sendo o plano B.

**Diagnóstico:** quando o app não consegue achar nenhum número de
capítulo (mesmo com seletor e navegador), a coluna "Cap. detectado" fica
com traço (—) e passa a guardar uma prévia do que foi recebido — passe o
mouse em cima do traço pra ver. Isso ajuda a identificar se o problema é
bloqueio, seletor errado, ou site fora do ar, sem precisar ficar
adivinhando.

## Notificações no Discord

### Configurar

1. No Discord: vá no canal onde quer receber os avisos.
2. Configurações do Canal → Integrações → Webhooks → Novo Webhook.
3. Copie a URL do Webhook.
4. Cole no app: em **Configurações**, no campo "Webhook do Discord"
   (webhook geral, usado por padrão por todos os grupos), ou no campo de
   webhook de um grupo específico (veja abaixo). Salvar.

Cada notificação inclui o nome do grupo, o número do capítulo (quando
identificado) e o link direto da fonte que detectou a mudança.

### Um canal por mangá

Dá pra ter um canal do Discord diferente pra cada mangá (ex: `#one-piece`,
`#dandadan`, `#gachiakuta`). Crie um webhook em cada canal e:

- Cole no campo **"Webhook do Discord deste grupo"** ao cadastrar a
  primeira fonte daquele grupo, ou
- Edite depois: cada grupo já cadastrado mostra um campo de webhook próprio
  no topo da seção dele na tela inicial — colar e clicar em "Salvar
  webhook".

Grupos sem webhook próprio caem no webhook geral configurado em
"Configurações".

### Testar

Tanto em Configurações (webhook geral) quanto em cada grupo na tela
inicial tem um botão **"Enviar teste"** / **"Testar"**, que manda uma
mensagem de exemplo pro Discord na hora — não precisa esperar um capítulo
de verdade sair pra confirmar que a URL está certa.

## Histórico de capítulos

Na aba **"Histórico"** (menu do topo) fica registrado, pra cada capítulo
novo detectado: quando foi, de qual mangá/grupo, qual o número do capítulo
(quando identificado) e o link direto da fonte. Só o registro do evento —
não guarda o conteúdo do capítulo em si. Mostra os últimos 200 eventos, e
ocupa muito pouco espaço (é só texto, sem imagens).

## Aviso de fonte com problema

Se uma fonte falhar 5 checagens seguidas (site fora do ar, mudou de
estrutura, bloqueou o acesso etc.):

- Aparece um selo **⚠️ com o número de falhas** ao lado do nome da fonte na
  tela inicial (passe o mouse pra ver a última mensagem de erro).
- O app manda um aviso automático no Discord (no canal daquele grupo)
  dizendo que a fonte está falhando.

Isso avisa só uma vez — não repete a cada checagem que falha. Assim que a
fonte voltar a funcionar, o contador zera e o aviso some sozinho.

## Dashboard rápido

No topo da tela inicial tem um resumo com: quantos mangás (grupos) você
tem cadastrados, quantas fontes estão ativas, quantos capítulos foram
detectados na última semana, e quantas fontes estão com problema no
momento.

## Versão no rodapé

No rodapé de todas as páginas aparece, bem discreto, algo como
`v2026.11.25 · a1b2c3d`. A data é a da última atualização entregue e o
código depois do ponto muda sozinho sempre que qualquer arquivo do app
mudar. Se o número não mudou depois de uma atualização, o servidor ainda
está rodando a versão antiga (falta o `git pull` e o rebuild da stack).

Além do rodapé, na **primeira página que você abrir depois de uma
atualização** aparece uma faixa azul: "✓ App atualizado para vX (antes:
vY)". Ela aparece uma vez só por usuário e some nas páginas seguintes. Se a
faixa não apareceu, o servidor ainda não está com a versão nova.

## Observações importantes

- Não configure intervalos muito curtos (ex: minutos) — isso pode
  sobrecarregar o site de origem e correr o risco de ser bloqueado por IP.
- Baixar/armazenar o conteúdo dos capítulos não é um recurso desse app (é
  uma questão de direitos autorais) — as notificações sempre levam pro link
  original da fonte.
