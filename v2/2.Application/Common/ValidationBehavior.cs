using Domain.Shared.Messaging;
using Domain.Shared.Responses;
using FluentValidation;
using Microsoft.Extensions.Logging;

namespace Application.Common;

/// <summary>
/// Segundo passo do pipeline: roda os validadores do pedido e, se algum reclamar, devolve as
/// chaves de erro sem chegar ao handler. A mensagem de cada regra já é a chave
/// <c>@err.*</c> que o front traduz.
/// </summary>
public sealed class ValidationBehavior<TRequest, TResponse> : IPipelineBehavior<TRequest, TResponse>
    where TRequest : IRequest<TResponse>
    where TResponse : Response, new()
{
    // Cada tipo fechado deste behavior tem o seu próprio campo estático, então o aviso de
    // "pedido sem validador" sai uma vez por tipo de pedido e não a cada chamada.
    private static int _missingValidatorLogged;

    private readonly IEnumerable<IValidator<TRequest>> _validators;
    private readonly ILogger<ValidationBehavior<TRequest, TResponse>> _logger;

    public ValidationBehavior(
        IEnumerable<IValidator<TRequest>> validators,
        ILogger<ValidationBehavior<TRequest, TResponse>> logger
    )
    {
        _validators = validators;
        _logger = logger;
    }

    public async Task<TResponse> Handle(
        TRequest request,
        RequestHandlerDelegate<TResponse> next,
        CancellationToken cancellationToken
    )
    {
        if (!_validators.Any())
        {
            // Não bloqueia: uma consulta por chave não tem o que validar. Só dá visibilidade
            // para o pedido que deveria ter validador e alguém esqueceu.
            if (Interlocked.Exchange(ref _missingValidatorLogged, 1) == 0)
            {
                _logger.LogWarning(
                    "Nenhum validador registrado para o pedido {RequestType}.",
                    typeof(TRequest).Name
                );
            }

            return await next();
        }

        var context = new ValidationContext<TRequest>(request);
        var errors = new List<string>();

        foreach (var validator in _validators)
        {
            var result = await validator.ValidateAsync(context, cancellationToken);
            errors.AddRange(result.Errors.Select(failure => failure.ErrorMessage));
        }

        if (errors.Count == 0)
            return await next();

        var response = new TResponse();
        foreach (var error in errors.Distinct())
            response.AddError(error);

        return response;
    }
}
