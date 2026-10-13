using System.Collections.Concurrent;
using Domain.Shared.Messaging;
using Microsoft.Extensions.DependencyInjection;

namespace Application.Common.Messaging;

/// <summary>
/// O despachante. Acha o handler do pedido no container, embrulha nos behaviors registrados e
/// chama. Próprio, e não um pacote, porque o que se usa de um mediador cabe neste arquivo e
/// assim o domínio compartilhado não depende de ninguém.
///
/// O tipo concreto do pedido só é conhecido em tempo de execução, então a ponte para os
/// genéricos fechados é montada por reflexão uma vez por tipo e guardada.
/// </summary>
public sealed class Mediator : IMediator
{
    private static readonly ConcurrentDictionary<Type, object> RequestInvokers = new();
    private static readonly ConcurrentDictionary<Type, NotificationInvoker> NotificationInvokers =
        new();

    private readonly IServiceProvider _services;

    public Mediator(IServiceProvider services) => _services = services;

    public Task<TResponse> Send<TResponse>(
        IRequest<TResponse> request,
        CancellationToken cancellationToken = default
    )
    {
        ArgumentNullException.ThrowIfNull(request);

        var invoker =
            (RequestInvoker<TResponse>)
                RequestInvokers.GetOrAdd(
                    request.GetType(),
                    static type =>
                        Activator.CreateInstance(
                            typeof(RequestInvoker<,>).MakeGenericType(type, typeof(TResponse))
                        )!
                );

        return invoker.Invoke(request, _services, cancellationToken);
    }

    public Task Publish(INotification notification, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(notification);

        var invoker = NotificationInvokers.GetOrAdd(
            notification.GetType(),
            static type =>
                (NotificationInvoker)
                    Activator.CreateInstance(typeof(NotificationInvoker<>).MakeGenericType(type))!
        );

        return invoker.Invoke(notification, _services, cancellationToken);
    }

    private abstract class RequestInvoker<TResponse>
    {
        public abstract Task<TResponse> Invoke(
            IRequest<TResponse> request,
            IServiceProvider services,
            CancellationToken cancellationToken
        );
    }

    private sealed class RequestInvoker<TRequest, TResponse> : RequestInvoker<TResponse>
        where TRequest : IRequest<TResponse>
    {
        public override Task<TResponse> Invoke(
            IRequest<TResponse> request,
            IServiceProvider services,
            CancellationToken cancellationToken
        )
        {
            var typed = (TRequest)request;

            var handler =
                services.GetService<IRequestHandler<TRequest, TResponse>>()
                ?? throw new InvalidOperationException(
                    $"Nenhum handler registrado para {typeof(TRequest).Name}."
                );

            RequestHandlerDelegate<TResponse> next = () => handler.Handle(typed, cancellationToken);

            // De trás para frente: o primeiro registrado tem que ser o de fora, o primeiro a
            // rodar e o último a ver a resposta.
            foreach (
                var behavior in services
                    .GetServices<IPipelineBehavior<TRequest, TResponse>>()
                    .Reverse()
            )
            {
                var inner = next;
                next = () => behavior.Handle(typed, inner, cancellationToken);
            }

            return next();
        }
    }

    private abstract class NotificationInvoker
    {
        public abstract Task Invoke(
            INotification notification,
            IServiceProvider services,
            CancellationToken cancellationToken
        );
    }

    private sealed class NotificationInvoker<TNotification> : NotificationInvoker
        where TNotification : INotification
    {
        public override async Task Invoke(
            INotification notification,
            IServiceProvider services,
            CancellationToken cancellationToken
        )
        {
            // Um de cada vez e na ordem do registro: os ouvintes dividem o mesmo escopo, e o
            // contexto do EF que mora nele não aceita duas operações ao mesmo tempo.
            foreach (var handler in services.GetServices<INotificationHandler<TNotification>>())
                await handler.Handle((TNotification)notification, cancellationToken);
        }
    }
}
