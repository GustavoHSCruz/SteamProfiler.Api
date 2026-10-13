using Domain.Shared.Entities;
using Domain.Shared.Events;
using Domain.Shared.Interfaces;
using Domain.Shared.Messaging;
using Domain.Shared.Requests;
using Domain.Shared.Responses;
using Microsoft.Extensions.Logging;

namespace Application.Common;

/// <summary>
/// Um comando. Muda estado, sempre nos mesmos quatro passos:
/// <list type="number">
/// <item><see cref="BeforeChanges"/> lê o que precisa e recusa cedo, acumulando erro na resposta.</item>
/// <item><see cref="Changes"/> chama o método do agregado e devolve a entidade.</item>
/// <item>a unidade de trabalho salva, uma transação por comando.</item>
/// <item><see cref="AfterChanges"/> preenche a resposta e <see cref="CreateEvent"/> diz o que publicar.</item>
/// </list>
/// Quem escreve um comando preenche os passos e não reescreve a ordem.
/// </summary>
public abstract class Command<TEntity, TRequest, TResponse, TEvent>
    : ServiceHandler<TRequest, TResponse>
    where TEntity : Entity, IAggregateRoot
    where TRequest : CommandRequest<TResponse>
    where TResponse : CommandResponse, new()
    where TEvent : DomainEvent
{
    /// <summary>A mesma chave que a V1 devolve quando uma escrita não chega ao banco.</summary>
    public const string SaveFailed = "@err.save_failed";

    protected readonly IMediator _mediator;
    protected readonly IWriteRepository _repository;
    protected readonly IUnitOfWork _uow;
    protected readonly ILogger _logger;

    protected Command(
        IMediator mediator,
        IWriteRepository repository,
        IUnitOfWork uow,
        ILogger logger
    )
    {
        _mediator = mediator;
        _repository = repository;
        _uow = uow;
        _logger = logger;
    }

    protected abstract Task BeforeChanges(TRequest request, CancellationToken cancellationToken);

    protected abstract Task<TEntity> Changes(TRequest request, CancellationToken cancellationToken);

    protected virtual Task AfterChanges(TEntity entity, CancellationToken cancellationToken) =>
        Task.CompletedTask;

    protected virtual TEvent? CreateEvent(TEntity entity) => null;

    public override async Task<TResponse> Handle(
        TRequest request,
        CancellationToken cancellationToken
    )
    {
        TEntity entity;

        try
        {
            await BeforeChanges(request, cancellationToken);

            if (_response.Errors.Count > 0)
                return _response;

            entity = await Changes(request, cancellationToken);

            if (_response.Errors.Count > 0)
                return _response;

            await _uow.SaveChangesAsync(cancellationToken);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            // Quem chamou desistiu. Não é falha da escrita e não tem para quem responder.
            throw;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "O comando {Command} não foi salvo.", GetType().Name);
            _response.AddError(SaveFailed, 500);
            return _response;
        }

        // Daqui para baixo o dado já está no banco. O que falhar é registrado, mas a resposta
        // continua sendo de sucesso: dizer a quem chamou que a escrita falhou seria mentira, e
        // ele tentaria de novo.
        try
        {
            await AfterChanges(entity, cancellationToken);

            var @event = CreateEvent(entity);
            if (@event is not null)
                await _mediator.Publish(@event, cancellationToken);
        }
        catch (Exception ex)
        {
            _logger.LogError(
                ex,
                "O comando {Command} salvou, mas o que vinha depois falhou.",
                GetType().Name
            );
        }

        return _response;
    }
}
