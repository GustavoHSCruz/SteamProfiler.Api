using Domain.Core.Interfaces;
using Domain.Core.ValueObjects;

namespace Service.API.Common;

/// <summary>
/// O chamador, lido do pedido HTTP. O idioma vem de <c>?lang=</c>, como na V1, e não do
/// Accept-Language: quem escolhe o idioma do site é o leitor, num seletor, e o navegador dele
/// pode estar em outro.
/// </summary>
public sealed class HttpRequestContext : IRequestContext
{
    private readonly IHttpContextAccessor _accessor;

    public HttpRequestContext(IHttpContextAccessor accessor) => _accessor = accessor;

    public Language Language =>
        Language.Parse(_accessor.HttpContext?.Request.Query["lang"].ToString());
}
