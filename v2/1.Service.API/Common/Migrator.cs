using Infrastructure.Contexts;
using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

namespace Service.API.Common;

/// <summary>
/// O modo <c>migrate</c>: aplica as migrations pendentes e sai. É um passo do deploy, rodado
/// antes de a api nova subir, e nunca a subida da api: uma migration que falha não pode ser o
/// motivo de a api não voltar.
/// </summary>
public static class Migrator
{
    public static async Task<int> RunAsync(
        IServiceProvider services,
        CancellationToken cancellationToken = default
    )
    {
        await using var scope = services.CreateAsyncScope();
        var context = scope.ServiceProvider.GetRequiredService<WriteContext>();
        var logger = scope
            .ServiceProvider.GetRequiredService<ILoggerFactory>()
            .CreateLogger(nameof(Migrator));

        // Num banco recém-criado a tabela de histórico ainda não existe, e o provider do
        // Postgres descobre isso lendo a tabela e engolindo o erro. Funciona, mas escreve um
        // "fail" no log do primeiro deploy, que é o pior lugar para um alarme falso. Criada
        // antes, a leitura simplesmente volta vazia.
        await context.GetService<IHistoryRepository>().CreateIfNotExistsAsync(cancellationToken);

        var pending = (
            await context.Database.GetPendingMigrationsAsync(cancellationToken)
        ).ToList();

        if (pending.Count == 0)
        {
            logger.LogInformation("Nenhuma migration pendente.");
            return 0;
        }

        await context.Database.MigrateAsync(cancellationToken);

        logger.LogInformation(
            "{Count} migration(s) aplicada(s): {Migrations}",
            pending.Count,
            string.Join(", ", pending)
        );

        return 0;
    }
}
