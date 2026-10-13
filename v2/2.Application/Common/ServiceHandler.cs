using Domain.Shared.Messaging;
using Domain.Shared.Responses;

namespace Application.Common;

/// <summary>Base de todo handler: já nasce com a resposta que vai devolver.</summary>
public abstract class ServiceHandler<TRequest, TResponse> : IRequestHandler<TRequest, TResponse>
    where TRequest : IRequest<TResponse>
    where TResponse : Response, new()
{
    protected readonly TResponse _response = new();

    public abstract Task<TResponse> Handle(TRequest request, CancellationToken cancellationToken);
}
