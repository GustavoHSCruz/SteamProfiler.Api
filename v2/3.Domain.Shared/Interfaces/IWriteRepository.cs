using System.Linq.Expressions;
using Domain.Shared.Entities;

namespace Domain.Shared.Interfaces;

/// <summary>
/// Leitura rastreada e escrita. Nada daqui chega ao banco antes do
/// <see cref="IUnitOfWork.SaveChangesAsync"/> do comando.
/// </summary>
public interface IWriteRepository
{
    IQueryable<T> AsQueryable<T>()
        where T : Entity, IAggregateRoot;

    Task<bool> ExistAsync<T>(
        Expression<Func<T, bool>> predicate,
        CancellationToken cancellationToken,
        params Expression<Func<T, object>>[] includes
    )
        where T : Entity, IAggregateRoot;

    Task<T?> SingleAsync<T>(
        Expression<Func<T, bool>> predicate,
        CancellationToken cancellationToken,
        params Expression<Func<T, object>>[] includes
    )
        where T : Entity, IAggregateRoot;

    Task AddAsync<T>(T entity, CancellationToken cancellationToken)
        where T : Entity, IAggregateRoot;

    Task AddRangeAsync<T>(IEnumerable<T> entities, CancellationToken cancellationToken)
        where T : Entity, IAggregateRoot;

    /// <summary>
    /// Marca para remoção todas as linhas que satisfazem o predicado. A remoção acontece no
    /// próximo SaveChanges, dentro da mesma transação do comando.
    /// </summary>
    Task DeleteAsync<T>(Expression<Func<T, bool>> predicate, CancellationToken cancellationToken)
        where T : Entity, IAggregateRoot;
}
