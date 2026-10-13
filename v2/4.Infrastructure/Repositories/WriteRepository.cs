using System.Linq.Expressions;
using Domain.Shared.Entities;
using Domain.Shared.Interfaces;
using Infrastructure.Contexts;
using Microsoft.EntityFrameworkCore;

namespace Infrastructure.Repositories;

public class WriteRepository : IWriteRepository
{
    private readonly WriteContext _context;

    public WriteRepository(WriteContext context)
    {
        _context = context;
    }

    public IQueryable<T> AsQueryable<T>()
        where T : Entity, IAggregateRoot
    {
        return _context.Set<T>().AsQueryable();
    }

    public Task<bool> ExistAsync<T>(
        Expression<Func<T, bool>> predicate,
        CancellationToken cancellationToken,
        params Expression<Func<T, object>>[] includes
    )
        where T : Entity, IAggregateRoot
    {
        return WithIncludes(includes).AnyAsync(predicate, cancellationToken);
    }

    public Task<T?> SingleAsync<T>(
        Expression<Func<T, bool>> predicate,
        CancellationToken cancellationToken,
        params Expression<Func<T, object>>[] includes
    )
        where T : Entity, IAggregateRoot
    {
        return WithIncludes(includes).SingleOrDefaultAsync(predicate, cancellationToken);
    }

    public async Task AddAsync<T>(T entity, CancellationToken cancellationToken)
        where T : Entity, IAggregateRoot
    {
        await _context.Set<T>().AddAsync(entity, cancellationToken);
    }

    public Task AddRangeAsync<T>(IEnumerable<T> entities, CancellationToken cancellationToken)
        where T : Entity, IAggregateRoot
    {
        return _context.Set<T>().AddRangeAsync(entities, cancellationToken);
    }

    public async Task DeleteAsync<T>(
        Expression<Func<T, bool>> predicate,
        CancellationToken cancellationToken
    )
        where T : Entity, IAggregateRoot
    {
        var entities = await _context.Set<T>().Where(predicate).ToListAsync(cancellationToken);

        _context.Set<T>().RemoveRange(entities);
    }

    private IQueryable<T> WithIncludes<T>(Expression<Func<T, object>>[] includes)
        where T : Entity, IAggregateRoot
    {
        IQueryable<T> query = _context.Set<T>();

        foreach (var include in includes)
            query = query.Include(include);

        return query;
    }
}
