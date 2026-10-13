using Domain.Core.Interfaces;
using Domain.Shared.Entities;
using Domain.Shared.Interfaces;
using Microsoft.EntityFrameworkCore;

namespace Infrastructure.Contexts;

public class UnitOfWork : IUnitOfWork
{
    private readonly WriteContext _context;
    private readonly IClock _clock;

    public UnitOfWork(WriteContext context, IClock clock)
    {
        _context = context;
        _clock = clock;
    }

    public Task<int> SaveChangesAsync(CancellationToken cancellationToken = default)
    {
        TouchTimestamps();
        return _context.SaveChangesAsync(cancellationToken);
    }

    private void TouchTimestamps()
    {
        var now = _clock.UtcNow;

        foreach (var entry in _context.ChangeTracker.Entries<Entity>())
        {
            if (entry.State == EntityState.Added)
            {
                entry.Property(x => x.CreatedAt).CurrentValue = now;
                entry.Property(x => x.UpdatedAt).CurrentValue = now;
            }
            else if (entry.State == EntityState.Modified)
            {
                entry.Property(x => x.UpdatedAt).CurrentValue = now;
                entry.Property(x => x.CreatedAt).IsModified = false;
            }
        }
    }
}
