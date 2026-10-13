using Service.API.Common;

namespace Tests;

public class FolderRouteConventionTests
{
    [Theory]
    [InlineData("Service.API.Controllers", "api/v2")]
    [InlineData("Service.API.Controllers.Catalog", "api/v2/catalog")]
    [InlineData("Service.API.Controllers.Blog.Admin", "api/v2/blog/admin")]
    [InlineData("Somewhere.Else", "api/v2")]
    [InlineData(null, "api/v2")]
    public void The_prefix_is_the_folder_under_Controllers(string? @namespace, string prefix)
    {
        Assert.Equal(prefix, FolderRouteConvention.BuildPrefix(@namespace));
    }
}
