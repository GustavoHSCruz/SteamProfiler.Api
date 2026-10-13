using System.Net;

namespace Tests.Integration;

[Collection("api")]
public class HealthTests
{
    private readonly ApiFixture _api;

    public HealthTests(ApiFixture api) => _api = api;

    [Fact]
    public async Task Healthz_says_the_process_is_alive_and_nobody_caches_it()
    {
        var response = await _api.Client.GetAsync("/api/v2/healthz");

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("""{"status":"ok"}""", await response.Content.ReadAsStringAsync());
        Assert.True(response.Headers.CacheControl?.NoStore);
    }

    [Fact]
    public async Task Readyz_says_the_database_answers()
    {
        var response = await _api.Client.GetAsync("/api/v2/readyz");

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("""{"status":"ok"}""", await response.Content.ReadAsStringAsync());
    }

    [Fact]
    public async Task With_the_database_gone_the_api_is_still_alive_but_not_ready()
    {
        // Uma porta em que ninguém escuta. A api tem que subir do mesmo jeito: ela não abre
        // conexão na subida, e é isso que deixa o healthcheck do container independente do
        // Postgres.
        await using var factory = ApiFixture.Factory(
            "Host=127.0.0.1;Port=1;Database=nothing;Username=nobody;Timeout=2"
        );
        using var client = factory.CreateClient();

        var alive = await client.GetAsync("/api/v2/healthz");
        var ready = await client.GetAsync("/api/v2/readyz");

        Assert.Equal(HttpStatusCode.OK, alive.StatusCode);
        Assert.Equal(HttpStatusCode.ServiceUnavailable, ready.StatusCode);
        Assert.Equal("""{"errors":["@err.load"]}""", await ready.Content.ReadAsStringAsync());
    }
}
