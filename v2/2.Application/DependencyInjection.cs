using Application.Common;
using Application.Common.Messaging;
using Domain.Shared.Messaging;
using FluentValidation;
using Microsoft.Extensions.DependencyInjection;

namespace Application;

public static class DependencyInjection
{
    /// <summary>
    /// O despachante, os handlers, os validadores e o pipeline. A ordem dos behaviors aqui é
    /// a ordem em que eles rodam.
    /// </summary>
    public static IServiceCollection AddApplication(this IServiceCollection services)
    {
        var assembly = typeof(DependencyInjection).Assembly;

        services.AddMediator(assembly);

        services.AddTransient(typeof(IPipelineBehavior<,>), typeof(RequestBehavior<,>));
        services.AddTransient(typeof(IPipelineBehavior<,>), typeof(ValidationBehavior<,>));

        services.AddValidatorsFromAssembly(assembly);

        return services;
    }
}
