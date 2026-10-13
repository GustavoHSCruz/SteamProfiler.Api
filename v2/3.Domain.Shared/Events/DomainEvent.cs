using Domain.Shared.Messaging;

namespace Domain.Shared.Events;

/// <summary>Algo que aconteceu no domínio e já está salvo quando é publicado.</summary>
public abstract class DomainEvent : INotification { }
