using System.Linq.Expressions;
using Domain.Shared.Entities;

namespace Domain.Shared.Interfaces;

/// <summary>Leitura sem rastreamento. É o que as consultas recebem.</summary>
public interface IReadRepository
{
    IQueryable<T> AsQueryable<T>()
        where T : Entity, IAggregateRoot;

    Task<bool> ExistAsync<T>(
        Expression<Func<T, bool>> predicate,
        CancellationToken cancellationToken
    )
        where T : Entity, IAggregateRoot;

    Task<T?> SingleAsync<T>(
        Expression<Func<T, bool>> predicate,
        CancellationToken cancellationToken,
        params Expression<Func<T, object>>[] includes
    )
        where T : Entity, IAggregateRoot;
}
