using Application.Common.Messaging;
using Domain.Shared.Messaging;
using Microsoft.Extensions.DependencyInjection;

namespace Tests;

public class MediatorTests
{
    public sealed record Ping(string Text) : IRequest<string>;

    public sealed class PingHandler : IRequestHandler<Ping, string>
    {
        public Task<string> Handle(Ping request, CancellationToken cancellationToken) =>
            Task.FromResult($"pong {request.Text}");
    }

    public sealed record Orphan : IRequest<string>;

    public sealed record Happened(List<string> Log) : INotification;

    public sealed class FirstListener : INotificationHandler<Happened>
    {
        public Task Handle(Happened notification, CancellationToken cancellationToken)
        {
            notification.Log.Add("first");
            return Task.CompletedTask;
        }
    }

    public sealed class SecondListener : INotificationHandler<Happened>
    {
        public Task Handle(Happened notification, CancellationToken cancellationToken)
        {
            notification.Log.Add("second");
            return Task.CompletedTask;
        }
    }

    public sealed record Unheard : INotification;

    private sealed class Trace
    {
        public List<string> Steps { get; } = [];
    }

    private sealed class Outer(Trace trace) : IPipelineBehavior<Ping, string>
    {
        public async Task<string> Handle(
            Ping request,
            RequestHandlerDelegate<string> next,
            CancellationToken cancellationToken
        )
        {
            trace.Steps.Add("outer in");
            var response = await next();
            trace.Steps.Add("outer out");
            return response;
        }
    }

    private sealed class Inner(Trace trace) : IPipelineBehavior<Ping, string>
    {
        public async Task<string> Handle(
            Ping request,
            RequestHandlerDelegate<string> next,
            CancellationToken cancellationToken
        )
        {
            trace.Steps.Add("inner in");
            var response = await next();
            trace.Steps.Add("inner out");
            return response;
        }
    }

    private sealed class Refuses : IPipelineBehavior<Ping, string>
    {
        public Task<string> Handle(
            Ping request,
            RequestHandlerDelegate<string> next,
            CancellationToken cancellationToken
        ) => Task.FromResult("refused");
    }

    private static ServiceProvider Build(Action<IServiceCollection>? configure = null)
    {
        var services = new ServiceCollection();
        services.AddMediator(typeof(MediatorTests).Assembly);
        configure?.Invoke(services);
        return services.BuildServiceProvider();
    }

    [Fact]
    public async Task Send_reaches_the_handler_registered_by_the_scan()
    {
        await using var provider = Build();
        var mediator = provider.GetRequiredService<IMediator>();

        Assert.Equal("pong a", await mediator.Send(new Ping("a")));
    }

    [Fact]
    public async Task Behaviors_wrap_the_handler_in_registration_order()
    {
        var trace = new Trace();
        await using var provider = Build(services =>
        {
            services.AddSingleton(trace);
            services.AddTransient<IPipelineBehavior<Ping, string>, Outer>();
            services.AddTransient<IPipelineBehavior<Ping, string>, Inner>();
        });

        await provider.GetRequiredService<IMediator>().Send(new Ping("a"));

        Assert.Equal(["outer in", "inner in", "inner out", "outer out"], trace.Steps);
    }

    [Fact]
    public async Task A_behavior_that_does_not_call_next_answers_in_place_of_the_handler()
    {
        await using var provider = Build(services =>
            services.AddTransient<IPipelineBehavior<Ping, string>, Refuses>()
        );

        Assert.Equal("refused", await provider.GetRequiredService<IMediator>().Send(new Ping("a")));
    }

    [Fact]
    public async Task Send_without_a_handler_says_which_request_has_none()
    {
        await using var provider = Build();
        var mediator = provider.GetRequiredService<IMediator>();

        var error = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            mediator.Send(new Orphan())
        );

        Assert.Contains(nameof(Orphan), error.Message);
    }

    [Fact]
    public async Task Publish_calls_every_listener_one_after_the_other()
    {
        await using var provider = Build();
        var log = new List<string>();

        await provider.GetRequiredService<IMediator>().Publish(new Happened(log));

        Assert.Equal(["first", "second"], log.Order());
        Assert.Equal(2, log.Count);
    }

    [Fact]
    public async Task Publish_with_nobody_listening_is_not_an_error()
    {
        await using var provider = Build();

        await provider.GetRequiredService<IMediator>().Publish(new Unheard());
    }
}
