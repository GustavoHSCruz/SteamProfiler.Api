namespace Service.API.Common;

/// <summary>
/// O modo <c>probe</c>: pergunta ao próprio processo, pela porta local, se ele está vivo, e
/// sai com 0 ou 1. É o healthcheck do container. A imagem não tem shell nem wget, então quem
/// pergunta é o mesmo binário.
/// </summary>
public static class Probe
{
    public const string Path = "/api/v2/healthz";

    public static async Task<int> RunAsync()
    {
        var port =
            Environment
                .GetEnvironmentVariable("ASPNETCORE_HTTP_PORTS")
                ?.Split(';', StringSplitOptions.RemoveEmptyEntries)
                .FirstOrDefault()
            ?? "8080";

        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(3) };

        try
        {
            using var response = await client.GetAsync($"http://127.0.0.1:{port}{Path}");
            return response.IsSuccessStatusCode ? 0 : 1;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            return 1;
        }
    }
}
