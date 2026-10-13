namespace Domain.Shared.Messaging;

/// <summary>Quem atende um pedido. Cada pedido tem exatamente um.</summary>
public interface IRequestHandler<in TRequest, TResponse>
    where TRequest : IRequest<TResponse>
{
    Task<TResponse> Handle(TRequest request, CancellationToken cancellationToken);
}
