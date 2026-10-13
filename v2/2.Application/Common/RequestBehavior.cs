using Domain.Core.Interfaces;
using Domain.Shared.Messaging;
using Domain.Shared.Requests;
using Domain.Shared.Responses;

namespace Application.Common;

/// <summary>
/// Primeiro passo do pipeline: põe no pedido o que se sabe de quem chamou. Fica aqui, e não em
/// cada controller, para nenhum handler depender de alguém ter lembrado.
/// </summary>
public sealed class RequestBehavior<TRequest, TResponse> : IPipelineBehavior<TRequest, TResponse>
    where TRequest : Request<TResponse>
    where TResponse : Response
{
    private readonly IRequestContext _context;

    public RequestBehavior(IRequestContext context) => _context = context;

    public Task<TResponse> Handle(
        TRequest request,
        RequestHandlerDelegate<TResponse> next,
        CancellationToken cancellationToken
    )
    {
        request.SetLanguage(_context.Language.Code);

        return next();
    }
}
