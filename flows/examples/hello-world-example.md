# Hello World Flow

## Purpose

A minimal example flow demonstrating the NiFi Hub flow structure. It generates a FlowFile with a static message and logs its attributes.

## Components

- **GenerateFlowFile**: Generates a FlowFile every 5 seconds with the content "Hello, NiFi Hub!"
- **LogAttribute**: Logs all attributes and the payload of each incoming FlowFile at INFO level

## Required NARs

- `org.apache.nifi:nifi-standard-nar:2.8.0` (included with standard NiFi installations)

## Parameters

This flow does not use any parameters.

## Configuration

No additional configuration is required. Import the flow into NiFi and start the process group.

## Expected Behavior

Once started, the flow produces a log entry every 5 seconds showing the FlowFile attributes and the "Hello, NiFi Hub!" content.
