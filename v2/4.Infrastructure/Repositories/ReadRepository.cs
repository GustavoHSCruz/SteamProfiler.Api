using System.Linq.Expressions;
using Domain.Shared.Entities;
using Domain.Shared.Interfaces;
using Infrastructure.Contexts;
using Microsoft.EntityFrameworkCore;

namespace Infrastructure.Repositories;

public class ReadRepository : IReadRepository
{
    private readonly ReadContext _context;

    public ReadRepository(ReadContext context)
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
        CancellationToken cancellationToken
    )
        where T : Entity, IAggregateRoot
    {
        return _context.Set<T>().AnyAsync(predicate, cancellationToken);
    }

    public Task<T?> SingleAsync<T>(
        Expression<Func<T, bool>> predicate,
        CancellationToken cancellationToken,
        params Expression<Func<T, object>>[] includes
    )
        where T : Entity, IAggregateRoot
    {
        IQueryable<T> query = _context.Set<T>();

        foreach (var include in includes)
            query = query.Include(include);

        return query.SingleOrDefaultAsync(predicate, cancellationToken);
    }
}
