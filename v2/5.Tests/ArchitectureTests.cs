using System.Reflection;
using Application.Common.Messaging;
using Domain.Core.Interfaces;
using Domain.Shared.Entities;
using Infrastructure.Contexts;

namespace Tests;

/// <summary>
/// Para onde cada camada pode olhar. Está em teste, e não só no desenho, porque a referência
/// errada compila do mesmo jeito: basta alguém adicionar uma linha num csproj.
/// </summary>
public class ArchitectureTests
{
    private static readonly Assembly Shared = typeof(Entity).Assembly;
    private static readonly Assembly Core = typeof(IClock).Assembly;
    private static readonly Assembly App = typeof(Mediator).Assembly;
    private static readonly Assembly Infra = typeof(WriteContext).Assembly;

    private static string[] References(Assembly assembly) =>
        assembly.GetReferencedAssemblies().Select(reference => reference.Name!).ToArray();

    private static bool IsRuntime(string name) =>
        name is "System.Runtime" or "netstandard" or "mscorlib" || name.StartsWith("System.");

    [Fact]
    public void Domain_Shared_knows_nobody()
    {
        Assert.All(References(Shared), name => Assert.True(IsRuntime(name), name));
    }

    [Fact]
    public void Domain_Core_knows_only_Domain_Shared()
    {
        Assert.All(
            References(Core),
            name => Assert.True(IsRuntime(name) || name == Shared.GetName().Name, name)
        );
    }

    [Fact]
    public void Application_does_not_know_infrastructure_http_or_the_database()
    {
        var references = References(App);

        Assert.DoesNotContain(Infra.GetName().Name, references);
        Assert.DoesNotContain(
            references,
            name =>
                name.StartsWith("Microsoft.AspNetCore")
                || name.StartsWith("Microsoft.EntityFrameworkCore")
                || name.StartsWith("Npgsql")
        );
    }

    [Fact]
    public void Infrastructure_does_not_know_the_application()
    {
        Assert.DoesNotContain(App.GetName().Name, References(Infra));
    }
}
