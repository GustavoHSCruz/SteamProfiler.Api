using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.ApplicationModels;

namespace Service.API.Common;

/// <summary>
/// O prefixo da rota sai da pasta (namespace) do controller.
/// <list type="bullet">
/// <item><c>Service.API.Controllers</c> vira <c>api/v2/[controller]</c></item>
/// <item><c>Service.API.Controllers.Catalog</c> vira <c>api/v2/catalog/[controller]</c></item>
/// <item><c>Service.API.Controllers.Foo.Bar</c> vira <c>api/v2/foo/bar/[controller]</c></item>
/// </list>
/// Controller que declara um <c>[Route("algo")]</c> relativo ganha o prefixo da pasta na
/// frente: <c>[Route("apps")]</c> em Catalog vira <c>api/v2/catalog/apps</c>.
///
/// O caminho inteiro mora aqui, com <c>/api/v2</c> e tudo, e o nginx não tira nada antes de
/// entregar: assim o OpenAPI descreve o endereço que o mundo vê.
/// </summary>
public class FolderRouteConvention : IControllerModelConvention
{
    public const string Root = "api/v2";

    private const string ControllersMarker = ".Controllers";

    public void Apply(ControllerModel controller)
    {
        var prefix = BuildPrefix(controller.ControllerType.Namespace);

        foreach (var selector in controller.Selectors)
        {
            if (selector.AttributeRouteModel?.Template is { Length: > 0 })
            {
                selector.AttributeRouteModel = AttributeRouteModel.CombineAttributeRouteModel(
                    new AttributeRouteModel(new RouteAttribute(prefix)),
                    selector.AttributeRouteModel
                );
            }
            else
            {
                selector.AttributeRouteModel = new AttributeRouteModel(
                    new RouteAttribute($"{prefix}/[controller]")
                );
            }
        }
    }

    public static string BuildPrefix(string? @namespace)
    {
        if (string.IsNullOrEmpty(@namespace))
            return Root;

        var index = @namespace.IndexOf(ControllersMarker, StringComparison.Ordinal);
        if (index < 0)
            return Root;

        // Tudo que vem depois de ".Controllers" vira segmento de rota.
        var after = @namespace[(index + ControllersMarker.Length)..].Trim('.');
        if (string.IsNullOrEmpty(after))
            return Root;

        var folders = after
            .Split('.', StringSplitOptions.RemoveEmptyEntries)
            .Select(segment => segment.ToLowerInvariant());

        return $"{Root}/{string.Join('/', folders)}";
    }
}
