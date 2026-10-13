namespace Domain.Shared.Messaging;

/// <summary>O resto do caminho até o handler, do ponto de vista de um behavior.</summary>
public delegate Task<TResponse> RequestHandlerDelegate<TResponse>();

/// <summary>
/// Um passo em volta de todo pedido, antes do handler. Roda na ordem em que foi registrado e
/// decide se chama <c>next</c> ou devolve a resposta ali mesmo.
/// </summary>
public interface IPipelineBehavior<in TRequest, TResponse>
    where TRequest : notnull
{
    Task<TResponse> Handle(
        TRequest request,
        RequestHandlerDelegate<TResponse> next,
        CancellationToken cancellationToken
    );
}
