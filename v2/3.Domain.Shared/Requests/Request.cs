using Domain.Shared.Messaging;
using Domain.Shared.Responses;

namespace Domain.Shared.Requests;

/// <summary>
/// Base de todo pedido. O que vem do chamador e não do corpo (hoje, o idioma) fica atrás de
/// métodos e não de propriedades de propósito: propriedade pública seria preenchida pelo
/// model binding e apareceria no OpenAPI como se fosse parte do contrato.
/// </summary>
public abstract class Request<TResponse> : IRequest<TResponse>
    where TResponse : Response
{
    private string _language = "en";

    public void SetLanguage(string language) => _language = language;

    public string GetLanguage() => _language;
}
