namespace Domain.Shared.Messaging;

/// <summary>
/// A única porta entre quem pede e quem atende. O controller não conhece handler nenhum, só
/// isto.
/// </summary>
public interface IMediator
{
    Task<TResponse> Send<TResponse>(
        IRequest<TResponse> request,
        CancellationToken cancellationToken = default
    );

    Task Publish(INotification notification, CancellationToken cancellationToken = default);
}
