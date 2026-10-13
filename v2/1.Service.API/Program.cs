using Application;
using Domain.Core.Interfaces;
using Infrastructure;
using Infrastructure.Contexts;
using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;
using Service.API.Common;

// O mesmo binário, três serviços:
//
//   (sem argumento)   serve a api
//   migrate           aplica as migrations e sai; passo do deploy
//   probe             pergunta ao processo que está servindo se ele está vivo; healthcheck
//
// Um binário só porque a imagem é uma só: o que migra o banco é exatamente o código que vai
// ler esse banco logo depois, e não uma ferramenta à parte que pode estar em outra versão.
const string MigrateMode = "migrate";
const string ProbeMode = "probe";

var mode = args.FirstOrDefault();

if (mode == ProbeMode)
    return await Probe.RunAsync();

var builder = WebApplication.CreateBuilder(mode == MigrateMode ? args[1..] : args);

builder.Services.AddHttpContextAccessor();
builder.Services.AddScoped<IRequestContext, HttpRequestContext>();

builder.Services.AddApplication();
builder.Services.AddInfrastructure();

builder
    .Services.AddControllers(options =>
    {
        options.Conventions.Add(new FolderRouteConvention());
        options.ModelMetadataDetailsProviders.Add(new SnakeCaseBinding());
    })
    .AddJsonOptions(options => ApiJson.Configure(options.JsonSerializerOptions))
    .ConfigureApiBehaviorOptions(options =>
    {
        // Corpo que não fecha com o pedido. Sem isto o ASP.NET responde um ProblemDetails, que
        // é um terceiro formato de erro que o front não lê.
        options.InvalidModelStateResponseFactory = _ => new BadRequestObjectResult(
            new ErrorResponse(ApiErrors.BadBody)
        );
    });

builder.Services.ConfigureHttpJsonOptions(options => ApiJson.Configure(options.SerializerOptions));

builder.Services.AddOpenApi(options =>
    options.AddDocumentTransformer(
        (document, _, _) =>
        {
            document.Info.Title = "SteamProfiler API";
            document.Info.Version = "v2";
            return Task.CompletedTask;
        }
    )
);

var app = builder.Build();

if (mode == MigrateMode)
    return await Migrator.RunAsync(app.Services);

// Todo erro sai no formato do contrato, inclusive os que nenhum handler viu.
app.UseExceptionHandler(handler =>
    handler.Run(async context =>
    {
        context.Response.StatusCode = StatusCodes.Status500InternalServerError;
        context.Response.Headers.CacheControl = "no-store";
        await context.Response.WriteAsJsonAsync(new ErrorResponse(ApiErrors.Load));
    })
);

// E os que o roteamento resolveu sozinho, sem corpo: rota que não existe, verbo errado.
app.UseStatusCodePages(async statusContext =>
{
    var response = statusContext.HttpContext.Response;
    var error = response.StatusCode switch
    {
        StatusCodes.Status404NotFound or StatusCodes.Status405MethodNotAllowed => ApiErrors.NoRoute,
        StatusCodes.Status400BadRequest or StatusCodes.Status415UnsupportedMediaType =>
            ApiErrors.BadBody,
        _ => ApiErrors.Load,
    };

    response.Headers.CacheControl = "no-store";
    await response.WriteAsJsonAsync(new ErrorResponse(error));
});

// Vivo: o processo responde. Não olha dependência nenhuma de propósito, porque é isto que o
// healthcheck do container pergunta, e um Postgres lento não é motivo para matar a api.
app.MapGet(
        Probe.Path,
        (HttpContext context) =>
        {
            context.Response.Headers.CacheControl = "no-store";
            return Results.Ok(new HealthResponse("ok"));
        }
    )
    .WithName("Healthz");

// Pronto: o banco responde. É o que o deploy confere depois de subir.
app.MapGet(
        "/api/v2/readyz",
        async (HttpContext context, WriteContext database, CancellationToken cancellationToken) =>
        {
            context.Response.Headers.CacheControl = "no-store";

            return await database.Database.CanConnectAsync(cancellationToken)
                ? Results.Ok(new HealthResponse("ok"))
                : Results.Json(
                    new ErrorResponse(ApiErrors.Load),
                    statusCode: StatusCodes.Status503ServiceUnavailable
                );
        }
    )
    .WithName("Readyz");

// O contrato. Publicado também em produção: é dele que o front gera os tipos.
app.MapOpenApi("/api/v2/openapi.json");

app.MapControllers();

await app.RunAsync();

return 0;

/// <summary>A resposta de <c>healthz</c> e <c>readyz</c> quando está tudo bem.</summary>
public sealed record HealthResponse(string Status);

// Dá à suíte de integração um ponto de entrada para o WebApplicationFactory.
public partial class Program;
