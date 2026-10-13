using Domain.Shared.Interfaces;
using Domain.Shared.Requests;
using Domain.Shared.Responses;

namespace Application.Common;

/// <summary>
/// Uma consulta. Não muda estado: lê pelo repositório de leitura, sem rastreamento, e projeta
/// direto na resposta. Não carrega agregado.
/// </summary>
public abstract class QueryHandler<TRequest, TResponse> : ServiceHandler<TRequest, TResponse>
    where TRequest : QueryRequest<TResponse>
    where TResponse : QueryResponse, new()
{
    protected readonly IReadRepository _repository;

    protected QueryHandler(IReadRepository repository)
    {
        _repository = repository;
    }
}
