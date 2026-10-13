using System.Reflection;
using Domain.Shared.Messaging;
using Microsoft.Extensions.DependencyInjection;

namespace Application.Common.Messaging;

public static class MediatorRegistration
{
    /// <summary>
    /// Registra o despachante e todo handler de pedido e de aviso que existir nos assemblies
    /// dados. Handler é transitório: ele guarda a resposta que está montando, então cada
    /// pedido precisa do seu.
    /// </summary>
    public static IServiceCollection AddMediator(
        this IServiceCollection services,
        params Assembly[] assemblies
    )
    {
        services.AddScoped<IMediator, Mediator>();

        foreach (var type in assemblies.Distinct().SelectMany(assembly => assembly.GetTypes()))
        {
            if (!type.IsClass || type.IsAbstract || type.IsGenericTypeDefinition)
                continue;

            foreach (var contract in type.GetInterfaces().Where(IsHandlerContract))
                services.AddTransient(contract, type);
        }

        return services;
    }

    private static bool IsHandlerContract(Type contract)
    {
        if (!contract.IsGenericType)
            return false;

        var definition = contract.GetGenericTypeDefinition();
        return definition == typeof(IRequestHandler<,>)
            || definition == typeof(INotificationHandler<>);
    }
}
