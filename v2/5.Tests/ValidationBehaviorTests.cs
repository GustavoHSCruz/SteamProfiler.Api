using Application.Common;
using Domain.Shared.Requests;
using Domain.Shared.Responses;
using FluentValidation;
using Microsoft.Extensions.Logging.Abstractions;

namespace Tests;

public class ValidationBehaviorTests
{
    public sealed class Answer : CommandResponse
    {
        public string? Text { get; set; }
    }

    public sealed class Leave : CommandRequest<Answer>
    {
        public string? Text { get; set; }
    }

    private sealed class LeaveValidator : AbstractValidator<Leave>
    {
        public LeaveValidator()
        {
            RuleFor(x => x.Text).NotEmpty().WithMessage("@err.empty_body");
            RuleFor(x => x.Text).MinimumLength(10).WithMessage("@err.message_short");
        }
    }

    private static ValidationBehavior<Leave, Answer> Behavior(
        params IValidator<Leave>[] validators
    ) => new(validators, NullLogger<ValidationBehavior<Leave, Answer>>.Instance);

    [Fact]
    public async Task A_refused_request_never_reaches_the_handler_and_carries_the_keys()
    {
        var reached = false;

        var response = await Behavior(new LeaveValidator())
            .Handle(
                new Leave { Text = "" },
                () =>
                {
                    reached = true;
                    return Task.FromResult(new Answer());
                },
                CancellationToken.None
            );

        Assert.False(reached);
        Assert.Equal(["@err.empty_body", "@err.message_short"], response.Errors);
        Assert.Null(response.StatusCode);
    }

    [Fact]
    public async Task A_valid_request_goes_through()
    {
        var response = await Behavior(new LeaveValidator())
            .Handle(
                new Leave { Text = "long enough to pass" },
                () => Task.FromResult(new Answer { Text = "saved" }),
                CancellationToken.None
            );

        Assert.Empty(response.Errors);
        Assert.Equal("saved", response.Text);
    }

    [Fact]
    public async Task A_request_with_no_validator_goes_through()
    {
        var response = await Behavior()
            .Handle(
                new Leave(),
                () => Task.FromResult(new Answer { Text = "saved" }),
                CancellationToken.None
            );

        Assert.Equal("saved", response.Text);
    }

    [Fact]
    public async Task The_same_key_from_two_validators_is_reported_once()
    {
        var response = await Behavior(new LeaveValidator(), new LeaveValidator())
            .Handle(
                new Leave { Text = "short" },
                () => Task.FromResult(new Answer()),
                CancellationToken.None
            );

        Assert.Equal(["@err.message_short"], response.Errors);
    }
}
