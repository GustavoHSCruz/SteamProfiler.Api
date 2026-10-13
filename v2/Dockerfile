# Dois estágios: o SDK compila e fica para trás, e o que vai para o servidor é só o runtime
# com a api publicada.
#
# As duas tags têm o patch escrito, e não `10.0`: a tag flutuante fica guardada no cache de
# quem builda, e a imagem seguiria saindo com o runtime do dia em que alguém a baixou pela
# primeira vez. Para subir de patch, troque as duas aqui.
FROM mcr.microsoft.com/dotnet/sdk:10.0.401 AS build
WORKDIR /src

# Só o que o restore lê, antes do código: assim mexer num .cs não joga fora a camada dos
# pacotes.
COPY global.json Directory.Build.props Directory.Packages.props ./
COPY 1.Service.API/1.Service.API.csproj 1.Service.API/
COPY 2.Application/2.Application.csproj 2.Application/
COPY 3.Domain.Core/3.Domain.Core.csproj 3.Domain.Core/
COPY 3.Domain.Shared/3.Domain.Shared.csproj 3.Domain.Shared/
COPY 4.Infrastructure/4.Infrastructure.csproj 4.Infrastructure/
RUN dotnet restore 1.Service.API/1.Service.API.csproj

COPY 1.Service.API/ 1.Service.API/
COPY 2.Application/ 2.Application/
COPY 3.Domain.Core/ 3.Domain.Core/
COPY 3.Domain.Shared/ 3.Domain.Shared/
COPY 4.Infrastructure/ 4.Infrastructure/
RUN dotnet publish 1.Service.API/1.Service.API.csproj \
      -c Release -o /app --no-restore -p:UseAppHost=false

# Chiseled: sem shell, sem gerenciador de pacote, e já roda como o usuário `app`, sem root.
# `extra` porque traz ICU e tzdata, e esta api ordena e normaliza texto em cinco idiomas.
#
# O preço é não haver como abrir um shell aqui dentro, nem curl para o healthcheck. Por isso o
# próprio binário sabe se perguntar se está vivo: `dotnet Service.API.dll probe`.
FROM mcr.microsoft.com/dotnet/aspnet:10.0.12-noble-chiseled-extra

LABEL org.opencontainers.image.source="https://github.com/GustavoHSCruz/SteamProfiler.Api" \
      org.opencontainers.image.licenses="MIT"

# GC de estação de trabalho. O de servidor abre um heap por núcleo e guarda memória para
# vazão que uma instância só, atrás de um nginx, não tem como usar; o servidor onde isto roda
# divide 8 GB com muita coisa.
ENV DOTNET_gcServer=0

WORKDIR /app
COPY --from=build /app .

EXPOSE 8080

# `docker compose run --rm api-v2 migrate` e o healthcheck `probe` entram como argumento
# depois disto. Sem argumento, serve.
ENTRYPOINT ["dotnet", "Service.API.dll"]
