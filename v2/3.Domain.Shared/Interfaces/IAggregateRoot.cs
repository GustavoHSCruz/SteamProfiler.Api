namespace Domain.Shared.Interfaces;

/// <summary>
/// Marca a entidade pela qual se entra num agregado. Os repositórios só aceitam estas: o que
/// está dentro de um agregado é lido e escrito pela raiz.
/// </summary>
public interface IAggregateRoot { }
