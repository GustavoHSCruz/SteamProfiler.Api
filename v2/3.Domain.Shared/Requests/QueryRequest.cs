using Domain.Shared.Responses;

namespace Domain.Shared.Requests;

/// <summary>Um pedido que só lê.</summary>
public abstract class QueryRequest<TResponse> : Request<TResponse>
    where TResponse : QueryResponse { }
