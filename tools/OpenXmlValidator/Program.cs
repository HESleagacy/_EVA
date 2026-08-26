using DocumentFormat.OpenXml.Packaging;
using DocumentFormat.OpenXml.Validation;

if (args.Length != 1)
{
    Console.Error.WriteLine("Usage: OpenXmlValidator <pptx>");
    return 2;
}

try
{
    using PresentationDocument document = PresentationDocument.Open(args[0], false);
    OpenXmlValidator validator = new();
    List<ValidationErrorInfo> errors = validator.Validate(document).ToList();
    foreach (ValidationErrorInfo error in errors)
    {
        Console.WriteLine($"{error.Path?.XPath}: {error.Description}");
    }

    Console.WriteLine($"Validation errors: {errors.Count}");
    return errors.Count == 0 ? 0 : 1;
}
catch (Exception exception)
{
    Console.Error.WriteLine(exception.Message);
    return 2;
}
