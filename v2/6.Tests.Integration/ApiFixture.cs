using Application.Common.Messaging;
using FluentValidation;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.AspNetCore.TestHost;
using Microsoft.Extensions.DependencyInjection;
using Service.API.Common;
using Testcontainers.PostgreSql;

namespace Tests.Integration;

/// <summary>
/// O que toda a suíte de integração divide: um PostgreSQL de verdade num container e a api de
/// pé por cima dele. Banco em memória não entra aqui de propósito, porque o que esta suíte
/// existe para pegar é justamente o que só o Postgres faz.
///
/// As migrations são aplicadas pelo mesmo <see cref="Migrator"/> que o deploy roda, antes de
/// a api atender, que é a ordem de produção.
/// </summary>
public sealed class ApiFixture : IAsyncLifetime
{
    // A mesma imagem do docker-compose.yml.
    private readonly PostgreSqlContainer _db = new PostgreSqlBuilder("postgres:18-alpine").Build();

    private WebApplicationFactory<Program> _factory = default!;

    public HttpClient Client { get; private set; } = default!;

    public string ConnectionString => _db.GetConnectionString();

    public IServiceProvider Services => _factory.Services;

    public async Task InitializeAsync()
    {
        await _db.StartAsync();

        _factory = Factory(ConnectionString);

        await Migrator.RunAsync(_factory.Services);

        Client = _factory.CreateClient();
    }

    /// <summary>
    /// A api apontada para o banco que for. Além do que ela já tem, ganha os pedidos e o
    /// controller de <c>Controllers/Mold</c>, que só existem nesta suíte e servem para
    /// atravessar o caminho inteiro (rota, despachante, pipeline, handler, resposta) antes
    /// de existir a primeira rota de verdade.
    /// </summary>
    public static WebApplicationFactory<Program> Factory(string connectionString)
    {
        var tests = typeof(ApiFixture).Assembly;

        return new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
        {
            builder.UseEnvironment("Testing");
            builder.UseSetting("DB_CONNECTION_STRING", connectionString);

            builder.ConfigureTestServices(services =>
            {
                services.AddMediator(tests);
                services.AddValidatorsFromAssembly(tests);
                services.AddControllers().AddApplicationPart(tests);
            });
        });
    }

    public async Task DisposeAsync()
    {
        Client?.Dispose();
        if (_factory is not null)
            await _factory.DisposeAsync();
        await _db.DisposeAsync();
    }
}

/// <summary>Um container e um host para todos os testes da coleção "api".</summary>
[CollectionDefinition("api")]
public sealed class ApiCollection : ICollectionFixture<ApiFixture> { }
