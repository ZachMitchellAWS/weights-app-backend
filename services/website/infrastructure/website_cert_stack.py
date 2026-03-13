"""Website ACM Certificate Stack (us-east-1).

CloudFront requires certificates in us-east-1. This stack creates the ACM
certificate with DNS validation via the Route53 hosted zone. After first
deploy, copy the certificate ARN to config CLOUDFRONT_CERT_ARN.
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    aws_certificatemanager as acm,
    aws_route53 as route53,
    aws_wafv2 as wafv2,
)
from constructs import Construct

from config import base


class WebsiteCertStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        project_name: str,
        env_name: str,
        config,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.project_name = project_name
        self.env_name = env_name
        self.config = config

        hosted_zone = route53.HostedZone.from_lookup(
            self,
            "HostedZone",
            domain_name=base.DOMAIN_NAME,
        )

        self.certificate = self._create_certificate(hosted_zone)

        # WAF IP whitelist for staging only
        self.web_acl_arn = None
        if self.config.WEBSITE_SUBDOMAIN:
            self.web_acl_arn = self._create_waf()

    def _create_certificate(self, hosted_zone) -> acm.Certificate:
        """Create ACM certificate for website domains."""
        if self.config.WEBSITE_SUBDOMAIN:
            # Staging: single subdomain
            domain_name = f"{self.config.WEBSITE_SUBDOMAIN}.{base.DOMAIN_NAME}"
            subject_alternative_names = None
        else:
            # Production: apex + www
            domain_name = base.DOMAIN_NAME
            subject_alternative_names = [f"www.{base.DOMAIN_NAME}"]

        certificate = acm.Certificate(
            self,
            "WebsiteCertificate",
            domain_name=domain_name,
            subject_alternative_names=subject_alternative_names,
            validation=acm.CertificateValidation.from_dns(hosted_zone),
        )

        CfnOutput(
            self,
            "CertificateArn",
            value=certificate.certificate_arn,
            description="ACM certificate ARN for CloudFront (copy to config CLOUDFRONT_CERT_ARN)",
        )

        return certificate

    def _create_waf(self) -> str:
        """Create WAF Web ACL with IP whitelist for staging."""
        ip_set = wafv2.CfnIPSet(
            self,
            "WhitelistIpSet",
            name=f"{self.project_name}-{self.env_name}-website-whitelist",
            scope="CLOUDFRONT",
            ip_address_version="IPV4",
            addresses=[],
        )

        web_acl = wafv2.CfnWebACL(
            self,
            "WebsiteWaf",
            name=f"{self.project_name}-{self.env_name}-website-waf",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(block={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=f"{self.project_name}-{self.env_name}-website-waf",
                sampled_requests_enabled=True,
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="AllowWhitelistedIPs",
                    priority=0,
                    action=wafv2.CfnWebACL.RuleActionProperty(allow={}),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name=f"{self.project_name}-{self.env_name}-whitelist-rule",
                        sampled_requests_enabled=True,
                    ),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        ip_set_reference_statement=wafv2.CfnWebACL.IPSetReferenceStatementProperty(
                            arn=ip_set.attr_arn,
                        ),
                    ),
                ),
            ],
        )

        CfnOutput(
            self,
            "WafWebAclArn",
            value=web_acl.attr_arn,
            description="WAF Web ACL ARN for CloudFront",
        )

        CfnOutput(
            self,
            "WafIpSetId",
            value=ip_set.attr_id,
            description="WAF IP Set ID (for whitelist-ip-staging)",
        )

        return web_acl.attr_arn
