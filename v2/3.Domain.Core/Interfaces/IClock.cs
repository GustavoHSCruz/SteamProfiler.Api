namespace Domain.Core.Interfaces;

/// <summary>A hora, pedida a alguém que um teste pode substituir.</summary>
public interface IClock
{
    DateTime UtcNow { get; }
}
