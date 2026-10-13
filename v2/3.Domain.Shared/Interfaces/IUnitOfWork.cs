namespace Domain.Shared.Interfaces;

/// <summary>Uma transação por comando.</summary>
public interface IUnitOfWork
{
    Task<int> SaveChangesAsync(CancellationToken cancellationToken = default);
}
