using System.Text.Json;
using Domain.Shared.Requests;
using Microsoft.AspNetCore.Mvc.ModelBinding.Metadata;

namespace Service.API.Common;

/// <summary>
/// Faz a query string falar o mesmo dialeto do corpo. O JSON já sai em snake_case pela
/// política do serializador, mas parâmetro de query não passa por ele: sem isto um pedido com
/// <c>PageSize</c> seria lido de <c>?PageSize=</c> e apareceria assim no OpenAPI, e o front
/// teria que saber de cor qual campo é de qual lado.
///
/// Vale só para as propriedades dos pedidos. Dar nome de binder a toda propriedade que o MVC
/// enxerga faz ele tentar preencher as do próprio controller (<c>HttpContext</c>, <c>User</c>)
/// a partir da query, e todo pedido morre antes de chegar à action.
/// </summary>
public sealed class SnakeCaseBinding : IBindingMetadataProvider
{
    public void CreateBindingMetadata(BindingMetadataProviderContext context)
    {
        if (context.Key.MetadataKind != ModelMetadataKind.Property || context.Key.Name is null)
            return;

        if (!IsRequest(context.Key.ContainerType))
            return;

        // Quem deu nome com [FromQuery(Name = "...")] sabe o que quer.
        context.BindingMetadata.BinderModelName ??= JsonNamingPolicy.SnakeCaseLower.ConvertName(
            context.Key.Name
        );
    }

    private static bool IsRequest(Type? type)
    {
        for (; type is not null; type = type.BaseType)
        {
            if (type.IsGenericType && type.GetGenericTypeDefinition() == typeof(Request<>))
                return true;
        }

        return false;
    }
}
