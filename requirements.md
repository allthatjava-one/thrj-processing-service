This is a backend python service that provides an API for a frontend application. The service will be built using the Cloudflare serverless platform.

This application will be serverless and deployed on Cloudflare Workers. The service will be stateless.

# The service will have the following endpoints:
- `POST /api/v1/pdf-compressor`: This endpoint will accept a presigned URL and make a call processoring service to compress the PDF file located at the presigned URL. The endpoint will return a new presigned URL for the compressed PDF file.
- `POST /api/v1/pdf-merger`: This endpoint will accept a list of objectKeys and send it to the merger service to merge the PDF files. The endpoint will return a new presigned URL for the merged PDF file. merge service end point will be set in environment variable `SERVICE_PDF_MERGE_URL`.

NOTE: For the first implementation, we will mock the PDF compression process and return a new presigned URL without actually compressing the file. In future iterations, we can integrate with a real PDF compression service. So, for the development phase, we will return the PDF file from the original presigned URL as the compressed file.

# Technical Requirements:
- The service must be built using Python.
- The service must be stateless.
- The service must be deployed on Cloudflare Workers.
- It should only accept requests from the limited external URLs that we will specify in the code for security reasons.
- The service should handle errors gracefully and return appropriate HTTP status codes and messages.
- The returning pdf file name should be "originalfilename-compressed.pdf" where "originalfilename" is the name of the original PDF file without the extension.

