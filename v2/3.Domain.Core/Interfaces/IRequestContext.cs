using Domain.Core.ValueObjects;

namespace Domain.Core.Interfaces;

/// <summary>
/// O que se sabe de quem chamou, sem que a aplicação precise saber que existe HTTP. Quem
/// implementa é a camada da API.
/// </summary>
public interface IRequestContext
{
    Language Language { get; }
}
