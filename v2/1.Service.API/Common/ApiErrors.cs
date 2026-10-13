namespace Service.API.Common;

/// <summary>
/// As chaves de erro que a própria camada HTTP devolve. São as mesmas <c>@err.*</c> que a V1
/// já usa e o front já traduz: chave nova aqui é string nova no repositório de idiomas.
/// </summary>
public static class ApiErrors
{
    public const string NoRoute = "@err.no_route";
    public const string BadBody = "@err.bad_body";
    public const string Load = "@err.load";
}

/// <summary>O formato de todo erro da V2: <c>{ "errors": ["@err.chave"] }</c>.</summary>
public sealed record ErrorResponse(IReadOnlyList<string> Errors)
{
    public ErrorResponse(string error)
        : this([error]) { }
}
