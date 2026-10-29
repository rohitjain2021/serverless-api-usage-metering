# Serverless API Usage Metering

A serverless system for automated API usage metering, historical reporting, and billing analysis.

## Architecture

![AWS monthly usage reporting workflow: EventBridge Scheduler triggers Lambda to retrieve API Gateway usage data and store monthly CSV or JSON reports in an encrypted S3 bucket.](docs/images/aws-monthly-usage-reporting-workflow.png)

The diagram shows the proposed architecture for this project.
