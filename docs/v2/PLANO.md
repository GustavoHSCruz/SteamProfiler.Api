# Plano da V2 da API

Estado: planejamento. Nenhuma linha da V2 existe ainda.
Escrito em 13/10/2026.

## 1. O que está decidido

| Assunto | Decisão |
| --- | --- |
| Plataforma | .NET 10 (LTS, suporte até 14/11/2028), ASP.NET Core |
| Arquitetura | CQRS + DDD em camadas numeradas, com mediador, validadores e eventos de domínio |
| Escopo | Paridade primeiro: a V2 faz o que a V1 faz hoje. Funcionalidade nova só depois que a V1 sair |
| Banco | PostgreSQL com EF Core e migrations, um banco só |
| Convivência | `/api/v2` lado a lado com `/api`. O front migra tela a tela e cada passo volta atrás no nginx |
| Repositório | Este mesmo. A V1 ficou marcada na tag `v1.0.0` |
| Ciclo da V1 | Só correção de segurança a partir de 13/10/2026, por seis meses: até 13/04/2027 |

A consequência da última linha é o prazo do plano inteiro: a V2 precisa estar
atendendo todo o tráfego antes de 13/04/2027. Depois dessa data a V1 continua
funcionando, mas sem ninguém cuidando dela.

## 2. O que a V1 é, em números

É isso que "paridade" quer dizer.

- 18.160 linhas de Python em 26 módulos, só biblioteca padrão.
- Mais de 60 rotas, 15 delas de administração.
- 240 testes, mais três verificações que sobem o nginx de verdade.
- 8 arquivos SQLite, cerca de 1,1 GB:

| Arquivo | Tamanho | O que guarda |
| --- | --- | --- |
| `meta.db` | 1.018 MB | 42 mil jogos lidos da loja, 152 mil preços, o índice de busca com 181 mil apps, conjuntos de cartas, câmbio |
| `houses.db` | 90 MB | 268 mil publishers e developers e o que cada um publicou |
| `security.db` | 4,7 MB | evidência de requisições, limitada e sanitizada |
| `census.db` | 0,3 MB | contagem de tráfego sem identidade |
| `recados.db` | 0,2 MB | recados, posts do blog em vários idiomas e os votos |
| `proton.db` | 0,1 MB | relatos de compatibilidade com Linux |
| `bans.db` | 0,1 MB | endereços barrados |
| `blocks.db` | 0 MB | perfis que o serviço não desenha |

- Um processo, um lock em volta de cada montagem de perfil, cache em memória
  para o que é de pessoa e SQLite para o que é de jogo.

## 3. Arquitetura

### 3.1 Solução

Tudo dentro de `v2/`, ao lado da V1, que continua na raiz enquanto estiver no ar.

```
v2/
  SteamProfiler.slnx
  1.Service.API/          controllers, middleware, convenção de rota, OpenAPI
  2.Application/          Commands, Queries, Validators, EventHandlers, Common
  3.Domain.Core/          Entities, ValueObjects, Events, Requests, Responses, Services
  3.Domain.Shared/        Entity, IAggregateRoot, repositórios, unidade de trabalho, bases de request e response
  4.Infrastructure/       EF Core, Postgres, leitores da Steam, cache, renderizadores
  5.Tests/                unidade: domínio, validadores, renderizadores
  6.Tests.Integration/    a api de pé contra um Postgres de verdade (Testcontainers)
  Dockerfile
```

### 3.2 Camadas e para onde cada uma olha

```
1.Service.API  ->  2.Application  ->  3.Domain.Core  ->  3.Domain.Shared
       |                                    ^
       +------->  4.Infrastructure  --------+
```

- `Domain.Shared` não conhece ninguém. Tem `Entity`, `IAggregateRoot`,
  `IReadRepository`, `IWriteRepository`, `IUnitOfWork` e as bases
  `CommandRequest<T>`, `QueryRequest<T>`, `CommandResponse`, `QueryResponse`.
- `Domain.Core` tem as regras: agregados, objetos de valor, eventos, e as
  interfaces do que o domínio precisa de fora (`ISteamWebApi`, `ISteamStore`,
  `ISteamCommunity`, `IClock`, `IAddressHasher`).
- `Application` orquestra: um handler por caso de uso.
- `Infrastructure` implementa as interfaces. É a única camada que sabe que
  existe Postgres, HTTP e a chave da Steam.
- `Service.API` traduz HTTP para request e response. Controller não tem regra.

Um desvio consciente em relação ao projeto de referência: lá a camada de
aplicação referencia a de infraestrutura. Aqui as interfaces ficam no domínio e
só a API referencia a infraestrutura, na composição. Está na seção 9 para
confirmação.

### 3.3 CQRS

**Comando** muda estado. Segue o molde em quatro passos:

```
BeforeChanges   lê o que precisa e recusa cedo (acumula erro na response)
Changes         chama o método do agregado e devolve a entidade
SaveChanges     uma transação por comando, pela unidade de trabalho
AfterChanges    preenche a response; CreateEvent publica o evento de domínio
```

**Consulta** não muda estado. Recebe `IReadRepository`, lê sem rastreamento,
projeta direto no DTO de resposta. Não carrega agregado.

**Pipeline do mediador**, na ordem:

1. `RequestBehavior` - endereço do chamador (já com hash e sal), idioma, e
   quem é, no dia em que existir conta.
2. `ValidationBehavior` - FluentValidation. Request sem validador gera aviso
   uma vez por tipo, não erro.
3. `BudgetBehavior` - novo, não existe na referência. É o `guard.py`: cada
   request que pode gastar a chave da Steam declara o preço, e o que já está
   em cache custa o que custa. Fica no pipeline para nenhum handler esquecer.

### 3.4 Contextos e agregados

| Contexto | Vem de | Agregados e o que fazem | Natureza |
| --- | --- | --- | --- |
| Perfis | `fetch.py`, `inv.py`, `rep.py` | nenhum persistido: `SteamId`, `Library`, `Achievement` como objetos de valor | só consulta, cache em memória |
| Catálogo | `meta.py`, `cards.py`, `fx.py`, `houses.py`, `proton.py`, `art.py` | `App` (preços, catálogo), `House`, `CardSet`, `FxRate`, `ProtonReport` | consulta, mais comandos internos de sincronização |
| Comunidade | `store.py`, `blog.py` | `Message` (recado, com votos), `Post` (textos por idioma, votos) | o contexto com mais comando de verdade |
| Proteção | `guard.py`, `bans.py`, `blocks.py`, `security_log.py` | `Ban`, `Block`, `KeyBudget` | comandos e a consulta do portão |
| Censo | `census.py` | `Visitor`, `Epoch`, `Screen` | escrita em lote, consulta agregada |
| Embeds | `embed.py`, `og.py`, `sign.py`, `i18n_words.py` | nenhum: funções puras de dados para SVG e PNG | consulta |

Regras que hoje estão espalhadas e passam a morar no agregado:

- `Post.ToggleVote(addressHash)` - um voto por endereço, votar de novo desfaz,
  rascunho não recebe voto.
- `Message.Leave(...)` - o pote de mel, o relógio e o teto.
- `Ban.Appeal(...)` - o único caminho aberto para quem está barrado.

### 3.5 Pacotes

| Para quê | Pacote |
| --- | --- |
| Web | ASP.NET Core 10, `Microsoft.AspNetCore.OpenApi` |
| Dados | EF Core 10, `Npgsql.EntityFrameworkCore.PostgreSQL` |
| Validação | FluentValidation 12 |
| Mediador | em aberto, ver seção 9 |
| Testes | xunit, `Microsoft.AspNetCore.Mvc.Testing`, `Testcontainers.PostgreSql` |
| Formatação | CSharpier, no pre-commit |

## 4. Banco

Um PostgreSQL, um schema por contexto: `catalog`, `community`, `protection`,
`census`. Perfis e Embeds não têm tabela.

**Migrations** pelo EF, aplicadas por um passo explícito do deploy, nunca na
subida da api: uma migration que falha não pode ser o motivo de a api não
voltar.

**Dados da V1.** Um importador de execução única (`v2/tools/import-v1`) lê os
SQLite e escreve no Postgres. Idempotente, pode rodar de novo.

| Origem | Destino | Como |
| --- | --- | --- |
| `recados.db` | `community` | importar tudo; é o único dado que não se refaz |
| `bans.db`, `blocks.db` | `protection` | importar tudo |
| `meta.db`, `houses.db` | `catalog` | importar; refazer do zero levaria meses de visitas |
| `proton.db` | `catalog` | importar |
| `census.db` | `census` | importar a semana corrente |
| `security.db` | - | não importar; começa vazio |

Busca: o índice `search_terms` (483 mil linhas) vira `pg_trgm` ou texto
completo do Postgres. É decisão da fase 2, medida contra a busca atual.

Enquanto as duas apis convivem, **cada tabela tem um dono só**. Um contexto
muda de dono inteiro, numa virada, e a V1 deixa de escrever naquele dado no
mesmo deploy. Duas apis escrevendo o mesmo voto é o defeito que este plano
existe para evitar.

## 5. Contrato

Raiz `/api/v2`, com o prefixo saindo da pasta do controller, como na referência.

| V1 | V2 |
| --- | --- |
| `GET /api/resolve?q=` | `GET /api/v2/profiles/resolve?q=` |
| `GET /api/profile?id=` | `GET /api/v2/profiles/{steamid}` |
| `GET /api/game?id=&appid=` | `GET /api/v2/profiles/{steamid}/games/{appid}` |
| `GET /api/unlocks?id=` | `GET /api/v2/profiles/{steamid}/unlocks` |
| `GET /api/mates?id=` | `GET /api/v2/profiles/{steamid}/mates` |
| `GET /api/wishlist`, `/inventory`, `/workshop` | `GET /api/v2/profiles/{steamid}/wishlist` etc. |
| `GET /api/game/public?appid=` | `GET /api/v2/catalog/apps/{appid}` |
| `GET /api/game/search?q=` | `GET /api/v2/catalog/apps?q=` |
| `GET /api/houses`, `/house` | `GET /api/v2/catalog/houses`, `/houses/{slug}` |
| `GET /api/blog`, `/blog/post` | `GET /api/v2/blog/posts`, `/posts/{key}` |
| `POST /api/blog/vote` | `POST /api/v2/blog/posts/{key}/vote` |
| `GET /api/board`, `POST /api/feedback` | `GET`, `POST /api/v2/board/messages` |
| `POST /api/vote` | `POST /api/v2/board/messages/{id}/vote` |
| `GET /api/status`, `/support`, `/owner` | `GET /api/v2/site/status` etc. |

Regras do contrato:

- **Erro** sempre como `{ "errors": ["@err.chave"] }`, com as mesmas chaves
  `@err.*` que o front já traduz. Status 400 por padrão, e o handler só troca
  quando o status faz parte do contrato.
- **OpenAPI** publicado em `/api/v2/openapi.json`. É o contrato; o front gera
  os tipos a partir dele.
- **Cache**: os mesmos `Cache-Control` e TTL da V1, rota a rota.

**Endereços que não podem morrer nunca.** Três famílias de URL da V1 estão
coladas em lugares que ninguém aqui controla, e continuam respondendo depois
que a V1 sair, atendidas pela V2 no caminho antigo:

1. Os embeds: `/api/bars.svg`, `/banner.svg`, `/badge.svg`, `/bars.txt`,
   `/versus.svg`, `/artwork.svg`, `/card.png`, `/og.png`. Estão em README,
   fórum e perfil da Steam.
2. `/api/companion` e `/api/companion/profile`: a extensão instalada chama
   esse endereço, e extensão instalada não se atualiza no nosso ritmo.
3. `/api/player`: o mesmo caso, para o player.

## 6. Convivência

No compose entram dois serviços: `db` (Postgres, volume próprio, sem porta
publicada) e `api-v2`. O nginx ganha um upstream e um bloco:

```
location /api/v2/ { ... proxy_pass http://sp_api_v2/; }
```

com os mesmos limites, o mesmo portão e as mesmas páginas de erro de `/api/`.

- **O portão continua na V1** até a fase 5. Banimento precisa de uma autoridade
  só, e trocá-la é o último passo, não o primeiro.
- **Captura sempre nomeada** em `location` com regex. Foi o `$1` zerado por um
  `map` que quebrou os três envios da V1 em outubro de 2026, e
  `tools/check-write-routing.sh` existe por causa disso. Cada bloco novo de
  `/api/v2` entra naquele teste.
- **Voltar atrás** é apontar a tela de volta para `/api/`. Por isso o front
  troca uma tela por vez, atrás de uma constante por tela, e não um cliente
  global.
- `.env` do servidor: toda variável nova de caminho ou bind entra lá antes do
  primeiro recreate.

## 7. Fases

Cada fase termina em produção, com critério de saída que se confere de fora.

| Fase | Entrega | Saiu quando |
| --- | --- | --- |
| 0. Fundação | solução, camadas, classes base, CI rodando `dotnet build` e `dotnet test` dentro do `check.sh`, Dockerfile em dois estágios, `db` e `api-v2` no compose, bloco no nginx, OpenAPI | `GET /api/v2/healthz` responde 200 em produção e o deploy sobe a V2 sem tocar na V1 |
| 1. Comunidade | blog, recados, votos, importador do `recados.db` | o blog e a página de recados do front leem e escrevem só em `/api/v2` |
| 2. Catálogo | loja, busca, cartas, câmbio, casas, Proton, arte; importador do `meta.db` e do `houses.db` | `/g/`, busca, publishers e developers servidos pela V2 |
| 3. Perfis | o leitor da Steam (`fetch.py`, 3.942 linhas), inventário, reputação, orçamento da chave | o perfil inteiro servido pela V2, com o orçamento diário contado num lugar só |
| 4. Embeds | SVG, PNG do link, cartão, assinatura | os endereços antigos de embed respondidos pela V2, iguais aos da V1 |
| 5. Proteção e censo | portão, armadilha, bans, apelação, censo, registro de segurança, rotas de administração | o `auth_request` do nginx aponta para a V2 e o painel lê dela |
| 6. Desligar a V1 | Companion e player atendidos pela V2, Python fora da raiz, tag `v2.0.0` | nenhum container Python no compose |

Ordem escolhida para errar barato: a fase 1 é o menor contexto que exercita o
molde inteiro (comando, validador, evento, migration, importador, virada de
dono). A fase 3 é a maior e a mais arriscada, e chega nela quem já fez o
caminho duas vezes.

## 8. Como se prova a paridade

- **Gravação e comparação.** Um conjunto fixo de perfis e jogos é pedido à V1,
  as respostas ficam gravadas, e a V2 tem que produzir o mesmo dado. Roda na
  suíte de integração.
- **Embeds por arquivo dourado.** SVG comparado byte a byte, PNG por pixel. A
  fonte bitmap desenhada à mão do `og.py` é portada glifo a glifo.
- **Os 240 testes da V1** são a especificação: cada um vira um teste na V2 ou
  uma linha explicando por que deixou de fazer sentido.
- **Postgres de verdade** na integração, não banco em memória.
- **Sombra** nas fases 2 e 3: a V2 recebe uma cópia das leituras e a diferença
  é registrada, sem que a resposta dela chegue a ninguém.

## 9. Em aberto

1. **Mediador e licença.** O MediatR mudou de licença a partir da versão 13:
   reciprocal ou comercial, com chave. Este repositório é MIT e público.
   Opções: MediatR 12 (Apache 2.0, a última nesse regime), um despachante
   próprio (umas 150 linhas, sem dependência), ou outro mediador MIT. Conferir
   os termos antes da fase 0.
2. **Aplicação sem referência à infraestrutura** (seção 3.2): confirmar o
   desvio em relação ao projeto de referência.
3. **Orçamento da chave na convivência.** Enquanto as duas apis gastam a mesma
   chave da Steam, o teto diário precisa ser contado num lugar só. Proposta: a
   V1 segue dona do orçamento até a fase 3 e a V2 pergunta a ela.
4. **Cache de perfil.** Memória do processo, como hoje, ou um cache distribuído.
   Proposta: memória, com `IMemoryCache` limitado por tamanho. Uma instância
   só não precisa de mais.
5. **Imagem base.** `aspnet:10.0` comum ou a variante chiseled, sem shell.
6. **Painel de administração.** Continua em Python falando com `/admin/*` até
   a fase 5, ou é reescrito junto. Proposta: continua.
7. **O que é correção de segurança na V1.** Proposta: vulnerabilidade, abuso e
   o que a Steam mudar e derrubar o site. Bug de comportamento fica para a V2.

## 10. Riscos

| Risco | O que segura |
| --- | --- |
| A V2 não assume tudo até 13/04/2027 | fases que entregam em produção; a decisão de estender a janela da V1 é tomada na fase 3, não no último mês |
| Dois donos para o mesmo dado | um dono por tabela, virada por contexto (seção 4) |
| Embed colado por aí muda de cara | arquivo dourado, e os endereços antigos nunca mudam |
| A reescrita do leitor da Steam perde um caso de borda | os 240 testes como especificação, mais a sombra |
| Recriar container no servidor sem a variável nova | a linha no `.env` antes do recreate, e a suíte valida o compose |
| Memória: Postgres e .NET ao lado do que já roda | limites no compose; medir na fase 0, antes de importar 1 GB |
