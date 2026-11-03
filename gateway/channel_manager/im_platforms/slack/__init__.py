"""Slack channel integration."""

from gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
)

__all__ = ["SlackChannel", "SlackChannelConfig"]
