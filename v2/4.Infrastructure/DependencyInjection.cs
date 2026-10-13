using Domain.Core.Interfaces;
using Domain.Shared.Interfaces;
using Infrastructure.Contexts;
using Infrastructure.Repositories;
using Infrastructure.Services;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Npgsql.EntityFrameworkCore.PostgreSQL.Infrastructure;

namespace Infrastructure;

public static class DependencyInjection
{
    public static IServiceCollection AddInfrastructure(this IServiceCollection services)
    {
        // O endereço do banco é lido quando o primeiro contexto é pedido, não aqui. Lido aqui,
        // ele seria fixado antes de a configuração terminar de ser montada, e a suíte de
        // integração não teria como apontar a api para o Postgres dela.
        services.AddDbContext<WriteContext>(
            (provider, options) =>
                options
                    .UseNpgsql(ConnectionString(provider), ConfigureNpgsql)
                    .UseSnakeCaseNamingConvention()
        );

        services.AddDbContext<ReadContext>(
            (provider, options) =>
                options
                    .UseNpgsql(ConnectionString(provider), ConfigureNpgsql)
                    .UseSnakeCaseNamingConvention()
                    .UseQueryTrackingBehavior(QueryTrackingBehavior.NoTracking)
        );

        services.AddScoped<IUnitOfWork, UnitOfWork>();
        services.AddScoped<IReadRepository, ReadRepository>();
        services.AddScoped<IWriteRepository, WriteRepository>();

        services.AddSingleton<IClock, SystemClock>();

        return services;
    }

    /// <summary>
    /// Os dois contextos têm o mesmo modelo e só um deles gera migration. Sem dizer isto, o
    /// EF procuraria as migrations no assembly de quem pediu.
    /// </summary>
    internal static void ConfigureNpgsql(NpgsqlDbContextOptionsBuilder npgsql)
    {
        npgsql.MigrationsAssembly(typeof(WriteContext).Assembly.GetName().Name);
    }

    private static string ConnectionString(IServiceProvider provider) =>
        DatabaseSettings.ConnectionString(provider.GetRequiredService<IConfiguration>());
}
