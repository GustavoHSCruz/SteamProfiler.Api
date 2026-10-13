using Domain.Shared.Responses;

namespace Domain.Shared.Requests;

/// <summary>Um pedido que muda estado.</summary>
public abstract class CommandRequest<TResponse> : Request<TResponse>
    where TResponse : CommandResponse { }
