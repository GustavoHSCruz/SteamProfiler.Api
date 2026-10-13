using System.Text.Json.Serialization;

namespace Domain.Shared.Responses;

/// <summary>
/// O que todo handler devolve. Erro não é exceção: é uma chave <c>@err.*</c> acumulada aqui,
/// que o front traduz. O controller olha <see cref="Errors"/> e decide o status.
/// </summary>
public abstract class Response
{
    private readonly List<string> _errors = [];

    [JsonIgnore]
    public IReadOnlyList<string> Errors => _errors;

    /// <summary>
    /// Só é preenchido quando o status faz parte do contrato. Sem ele, erro é 400 e sucesso é
    /// o que o verbo pede.
    /// </summary>
    [JsonIgnore]
    public int? StatusCode { get; private set; }

    public void AddError(string error, int? statusCode = null)
    {
        if (statusCode.HasValue)
            StatusCode = statusCode;

        _errors.Add(error);
    }

    public void SetStatusCode(int statusCode) => StatusCode = statusCode;
}
