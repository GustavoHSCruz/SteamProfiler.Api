using Application.Common;
using Domain.Core.Interfaces;
using Domain.Core.ValueObjects;
using Domain.Shared.Requests;
using Domain.Shared.Responses;

namespace Tests;

public class RequestBehaviorTests
{
    private sealed class Posts : QueryResponse { }

    private sealed class ListPosts : QueryRequest<Posts> { }

    private sealed class Caller(string? lang) : IRequestContext
    {
        public Language Language => Language.Parse(lang);
    }

    [Theory]
    [InlineData("pt", "pt")]
    [InlineData("ZH_CN", "zh-cn")]
    [InlineData("klingon", "en")]
    [InlineData(null, "en")]
    public async Task The_request_learns_the_language_before_the_handler_sees_it(
        string? asked,
        string served
    )
    {
        var request = new ListPosts();
        string? seen = null;

        await new RequestBehavior<ListPosts, Posts>(new Caller(asked)).Handle(
            request,
            () =>
            {
                seen = request.GetLanguage();
                return Task.FromResult(new Posts());
            },
            CancellationToken.None
        );

        Assert.Equal(served, seen);
    }
}
