using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;

namespace Tests.Integration;

/// <summary>
/// As regras do contrato que valem para toda rota, conferidas pelo fio: o formato do erro, o
/// snake_case, o prefixo que sai da pasta e o OpenAPI.
/// </summary>
[Collection("api")]
public class ContractTests
{
    private readonly ApiFixture _api;

    public ContractTests(ApiFixture api) => _api = api;

    private static async Task<string> Body(HttpResponseMessage response) =>
        await response.Content.ReadAsStringAsync();

    [Fact]
    public async Task A_controller_answers_under_the_prefix_of_its_folder_in_snake_case()
    {
        var response = await _api.Client.GetAsync("/api/v2/mold/echo?text=hi&lang=pt");

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        // O corpo inteiro, e não só os dois campos: `errors` e `status_code` são da resposta
        // por dentro e não podem vazar para quem chama.
        Assert.Equal("""{"text":"hi","served_in":"pt"}""", await Body(response));
    }

    [Fact]
    public async Task The_query_string_is_snake_case_like_the_body()
    {
        var response = await _api.Client.GetAsync("/api/v2/mold/echo?text=hi&repeat_times=3");

        Assert.Equal("""{"text":"hihihi","served_in":"en"}""", await Body(response));
    }

    [Fact]
    public async Task A_language_the_site_does_not_have_is_served_in_english()
    {
        var response = await _api.Client.GetAsync("/api/v2/mold/echo?text=hi&lang=tlh");

        Assert.Equal("""{"text":"hi","served_in":"en"}""", await Body(response));
    }

    [Fact]
    public async Task A_request_the_validator_refuses_is_a_400_with_the_key()
    {
        var response = await _api.Client.GetAsync("/api/v2/mold/echo");

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal("""{"errors":["@err.empty_body"]}""", await Body(response));
    }

    [Fact]
    public async Task A_handler_can_choose_the_status_when_it_is_part_of_the_contract()
    {
        var response = await _api.Client.GetAsync("/api/v2/mold/echo?text=missing");

        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
        Assert.Equal("""{"errors":["@err.no_post"]}""", await Body(response));
    }

    [Fact]
    public async Task A_post_that_worked_is_a_201()
    {
        var response = await _api.Client.PostAsJsonAsync("/api/v2/mold/echo", new { text = "hi" });

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        Assert.Equal("""{"text":"HI","served_in":"en"}""", await Body(response));
    }

    [Fact]
    public async Task A_body_that_is_not_json_is_a_400_in_the_same_shape()
    {
        var response = await _api.Client.PostAsync(
            "/api/v2/mold/echo",
            new StringContent("{ not json", Encoding.UTF8, "application/json")
        );

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal("""{"errors":["@err.bad_body"]}""", await Body(response));
    }

    [Fact]
    public async Task An_exception_nobody_caught_is_a_500_that_leaks_nothing()
    {
        var response = await _api.Client.GetAsync("/api/v2/mold/echo?text=boom");

        Assert.Equal(HttpStatusCode.InternalServerError, response.StatusCode);
        Assert.Equal("""{"errors":["@err.load"]}""", await Body(response));
    }

    [Theory]
    [InlineData("/api/v2/nothing-here")]
    [InlineData("/somewhere/else")]
    public async Task A_route_that_does_not_exist_is_a_404_in_json(string path)
    {
        var response = await _api.Client.GetAsync(path);

        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
        Assert.Equal("""{"errors":["@err.no_route"]}""", await Body(response));
        Assert.True(response.Headers.CacheControl?.NoStore);
    }

    [Fact]
    public async Task The_wrong_verb_is_answered_in_json_too()
    {
        var response = await _api.Client.DeleteAsync("/api/v2/healthz");

        Assert.Equal(HttpStatusCode.MethodNotAllowed, response.StatusCode);
        Assert.Equal("""{"errors":["@err.no_route"]}""", await Body(response));
    }

    [Fact]
    public async Task The_OpenAPI_document_describes_the_public_paths()
    {
        var response = await _api.Client.GetAsync("/api/v2/openapi.json");

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);

        using var document = JsonDocument.Parse(await Body(response));
        var root = document.RootElement;

        Assert.Equal("v2", root.GetProperty("info").GetProperty("version").GetString());

        var paths = root.GetProperty("paths").EnumerateObject().Select(p => p.Name).ToList();
        Assert.Contains("/api/v2/healthz", paths);
        Assert.Contains("/api/v2/readyz", paths);
        Assert.Contains("/api/v2/mold/echo", paths);

        // O idioma viaja atrás de método no pedido, e não como propriedade, exatamente para
        // não aparecer aqui como se fosse parâmetro da rota.
        var parameters = root.GetProperty("paths")
            .GetProperty("/api/v2/mold/echo")
            .GetProperty("get")
            .GetProperty("parameters")
            .EnumerateArray()
            .Select(p => p.GetProperty("name").GetString())
            .ToList();
        Assert.Equal(["text", "repeat_times"], parameters);
    }
}
