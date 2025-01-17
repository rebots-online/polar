import uuid
from collections.abc import Sequence
from typing import Any

import stripe as stripe_lib
import structlog
from sqlalchemy import Select, UnaryExpression, asc, desc, select, update
from sqlalchemy.orm import contains_eager, joinedload, selectinload

from polar.auth.models import (
    Anonymous,
    AuthSubject,
    is_direct_user,
    is_organization,
    is_user,
)
from polar.checkout.schemas import (
    CheckoutConfirm,
    CheckoutCreate,
    CheckoutCreatePublic,
    CheckoutUpdate,
    CheckoutUpdatePublic,
)
from polar.checkout.tax import TaxID, to_stripe_tax_id, validate_tax_id
from polar.config import settings
from polar.custom_field.data import validate_custom_field_data
from polar.enums import PaymentProcessor
from polar.eventstream.service import publish
from polar.exceptions import PolarError, PolarRequestValidationError, ValidationError
from polar.integrations.stripe.schemas import ProductType
from polar.integrations.stripe.service import stripe as stripe_service
from polar.integrations.stripe.utils import get_expandable_id
from polar.kit.address import Address
from polar.kit.crypto import generate_token
from polar.kit.pagination import PaginationParams, paginate
from polar.kit.services import ResourceServiceReader
from polar.kit.sorting import Sorting
from polar.kit.utils import utc_now
from polar.logging import Logger
from polar.models import (
    Checkout,
    CheckoutLink,
    Organization,
    Product,
    ProductPriceCustom,
    ProductPriceFixed,
    Subscription,
    User,
    UserOrganization,
)
from polar.models.checkout import CheckoutStatus
from polar.models.product_price import ProductPriceAmountType, ProductPriceFree
from polar.models.webhook_endpoint import WebhookEventType
from polar.organization.service import organization as organization_service
from polar.postgres import AsyncSession
from polar.product.service.product_price import product_price as product_price_service
from polar.user.service.user import user as user_service
from polar.user_organization.service import (
    user_organization as user_organization_service,
)
from polar.webhook.service import webhook as webhook_service
from polar.worker import enqueue_job

from . import ip_geolocation
from .sorting import CheckoutSortProperty
from .tax import TaxCalculationError, calculate_tax

log: Logger = structlog.get_logger()


class CheckoutError(PolarError): ...


class PaymentError(CheckoutError):
    def __init__(
        self, checkout: Checkout, error_type: str | None, error: str | None
    ) -> None:
        self.checkout = checkout
        self.error_type = error_type
        self.error = error
        message = (
            f"The payment failed{f': {error}' if error else '.'} "
            "Please try again with a different payment method."
        )
        super().__init__(message, 400)


class CheckoutDoesNotExist(CheckoutError):
    def __init__(self, checkout_id: uuid.UUID) -> None:
        self.checkout_id = checkout_id
        message = f"Checkout {checkout_id} does not exist."
        super().__init__(message)


class NotOpenCheckout(CheckoutError):
    def __init__(self, checkout: Checkout) -> None:
        self.checkout = checkout
        self.status = checkout.status
        message = f"Checkout {checkout.id} is not open: {checkout.status}"
        super().__init__(message, 403)


class NotConfirmedCheckout(CheckoutError):
    def __init__(self, checkout: Checkout) -> None:
        self.checkout = checkout
        self.status = checkout.status
        message = f"Checkout {checkout.id} is not confirmed: {checkout.status}"
        super().__init__(message)


class PaymentIntentNotSucceeded(CheckoutError):
    def __init__(self, checkout: Checkout, payment_intent_id: str) -> None:
        self.checkout = checkout
        self.payment_intent_id = payment_intent_id
        message = (
            f"Payment intent {payment_intent_id} for {checkout.id} is not successful."
        )
        super().__init__(message)


class NoCustomerOnPaymentIntent(CheckoutError):
    def __init__(self, checkout: Checkout, payment_intent_id: str) -> None:
        self.checkout = checkout
        self.payment_intent_id = payment_intent_id
        message = (
            f"Payment intent {payment_intent_id} "
            f"for {checkout.id} has no customer associated."
        )
        super().__init__(message)


class NoPaymentMethodOnPaymentIntent(CheckoutError):
    def __init__(self, checkout: Checkout, payment_intent_id: str) -> None:
        self.checkout = checkout
        self.payment_intent_id = payment_intent_id
        message = (
            f"Payment intent {payment_intent_id} "
            f"for {checkout.id} has no payment method associated."
        )
        super().__init__(message)


class NoCustomerOnCheckout(CheckoutError):
    def __init__(self, checkout: Checkout) -> None:
        self.checkout = checkout
        message = f"{checkout.id} has no customer associated."
        super().__init__(message)


class NotAFreePrice(CheckoutError):
    def __init__(self, checkout: Checkout) -> None:
        self.checkout = checkout
        message = f"{checkout.id} is not a free price."
        super().__init__(message)


CHECKOUT_CLIENT_SECRET_PREFIX = "polar_c_"


class CheckoutService(ResourceServiceReader[Checkout]):
    async def list(
        self,
        session: AsyncSession,
        auth_subject: AuthSubject[User | Organization],
        *,
        organization_id: Sequence[uuid.UUID] | None = None,
        product_id: Sequence[uuid.UUID] | None = None,
        pagination: PaginationParams,
        sorting: list[Sorting[CheckoutSortProperty]] = [
            (CheckoutSortProperty.created_at, True)
        ],
    ) -> tuple[Sequence[Checkout], int]:
        statement = self._get_readable_checkout_statement(auth_subject)

        if organization_id is not None:
            statement = statement.where(Product.organization_id.in_(organization_id))

        if product_id is not None:
            statement = statement.where(Checkout.product_id.in_(product_id))

        order_by_clauses: list[UnaryExpression[Any]] = []
        for criterion, is_desc in sorting:
            clause_function = desc if is_desc else asc
            if criterion == CheckoutSortProperty.created_at:
                order_by_clauses.append(clause_function(Checkout.created_at))
            elif criterion == CheckoutSortProperty.expires_at:
                order_by_clauses.append(clause_function(Checkout.expires_at))
        statement = statement.order_by(*order_by_clauses)

        return await paginate(session, statement, pagination=pagination)

    async def get_by_id(
        self,
        session: AsyncSession,
        auth_subject: AuthSubject[User | Organization],
        id: uuid.UUID,
    ) -> Checkout | None:
        statement = self._get_readable_checkout_statement(auth_subject).where(
            Checkout.id == id
        )
        result = await session.execute(statement)
        return result.unique().scalar_one_or_none()

    async def create(
        self,
        session: AsyncSession,
        checkout_create: CheckoutCreate,
        auth_subject: AuthSubject[User | Organization],
        ip_geolocation_client: ip_geolocation.IPGeolocationClient | None = None,
    ) -> Checkout:
        price = await product_price_service.get_writable_by_id(
            session, checkout_create.product_price_id, auth_subject
        )

        if price is None:
            raise PolarRequestValidationError(
                [
                    {
                        "type": "value_error",
                        "loc": ("body", "product_price_id"),
                        "msg": "Price does not exist.",
                        "input": checkout_create.product_price_id,
                    }
                ]
            )

        if price.is_archived:
            raise PolarRequestValidationError(
                [
                    {
                        "type": "value_error",
                        "loc": ("body", "product_price_id"),
                        "msg": "Price is archived.",
                        "input": checkout_create.product_price_id,
                    }
                ]
            )

        product = price.product
        if product.is_archived:
            raise PolarRequestValidationError(
                [
                    {
                        "type": "value_error",
                        "loc": ("body", "product_price_id"),
                        "msg": "Product is archived.",
                        "input": checkout_create.product_price_id,
                    }
                ]
            )

        if checkout_create.amount is not None and isinstance(price, ProductPriceCustom):
            if (
                price.minimum_amount is not None
                and checkout_create.amount < price.minimum_amount
            ):
                raise PolarRequestValidationError(
                    [
                        {
                            "type": "greater_than_equal",
                            "loc": ("body", "amount"),
                            "msg": "Amount is below minimum.",
                            "input": checkout_create.amount,
                            "ctx": {"ge": price.minimum_amount},
                        }
                    ]
                )
            elif (
                price.maximum_amount is not None
                and checkout_create.amount > price.maximum_amount
            ):
                raise PolarRequestValidationError(
                    [
                        {
                            "type": "less_than_equal",
                            "loc": ("body", "amount"),
                            "msg": "Amount is above maximum.",
                            "input": checkout_create.amount,
                            "ctx": {"le": price.maximum_amount},
                        }
                    ]
                )

        customer_tax_id: TaxID | None = None
        if checkout_create.customer_tax_id is not None:
            if checkout_create.customer_billing_address is None:
                raise PolarRequestValidationError(
                    [
                        {
                            "type": "missing",
                            "loc": ("body", "customer_billing_address"),
                            "msg": "Country is required to validate tax ID.",
                            "input": None,
                        }
                    ]
                )
            try:
                customer_tax_id = validate_tax_id(
                    checkout_create.customer_tax_id,
                    checkout_create.customer_billing_address.country,
                )
            except ValueError as e:
                raise PolarRequestValidationError(
                    [
                        {
                            "type": "value_error",
                            "loc": ("body", "customer_tax_id"),
                            "msg": "Invalid tax ID.",
                            "input": checkout_create.customer_tax_id,
                        }
                    ]
                ) from e

        subscription: Subscription | None = None
        customer: User | None = None
        if checkout_create.subscription_id is not None:
            subscription = await self._get_upgradable_subscription(
                session, checkout_create.subscription_id, product.organization_id
            )
            if subscription is None:
                raise PolarRequestValidationError(
                    [
                        {
                            "type": "value_error",
                            "loc": ("body", "subscription_id"),
                            "msg": "Subscription does not exist.",
                            "input": checkout_create.subscription_id,
                        }
                    ]
                )
            if subscription.price.amount_type != ProductPriceAmountType.free:
                raise PolarRequestValidationError(
                    [
                        {
                            "type": "value_error",
                            "loc": ("body", "subscription_id"),
                            "msg": "Only free subscriptions can be upgraded.",
                            "input": checkout_create.subscription_id,
                        }
                    ]
                )
            customer = subscription.user

        product = await self._eager_load_product(session, product)

        amount = checkout_create.amount
        currency = None
        if isinstance(price, ProductPriceFixed):
            amount = price.price_amount
            currency = price.price_currency
        elif isinstance(price, ProductPriceCustom):
            currency = price.price_currency
            if amount is None:
                amount = price.preset_amount or 1000
        elif isinstance(price, ProductPriceFree):
            amount = None
            currency = None

        custom_field_data = validate_custom_field_data(
            product.attached_custom_fields, checkout_create.custom_field_data
        )

        checkout = Checkout(
            client_secret=generate_token(prefix=CHECKOUT_CLIENT_SECRET_PREFIX),
            amount=amount,
            currency=currency,
            product=product,
            product_price=price,
            customer_billing_address=checkout_create.customer_billing_address,
            customer_tax_id=customer_tax_id,
            subscription=subscription,
            customer=customer,
            custom_field_data=custom_field_data,
            **checkout_create.model_dump(
                exclude={
                    "product_price_id",
                    "amount",
                    "customer_billing_address",
                    "customer_tax_id",
                    "subscription_id",
                    "custom_field_data",
                },
                by_alias=True,
            ),
        )
        session.add(checkout)

        if checkout.customer is not None and checkout.customer_email is None:
            checkout.customer_email = checkout.customer.email

        checkout = await self._update_checkout_ip_geolocation(
            session, checkout, ip_geolocation_client
        )

        try:
            checkout = await self._update_checkout_tax(session, checkout)
        # Swallow incomplete tax calculation error: require it only on confirm
        except TaxCalculationError:
            pass

        await session.flush()
        await self._after_checkout_created(session, checkout)

        return checkout

    async def client_create(
        self,
        session: AsyncSession,
        checkout_create: CheckoutCreatePublic,
        auth_subject: AuthSubject[User | Anonymous],
        ip_geolocation_client: ip_geolocation.IPGeolocationClient | None = None,
        ip_address: str | None = None,
    ) -> Checkout:
        price = await product_price_service.get_by_id(
            session, checkout_create.product_price_id
        )

        if price is None:
            raise PolarRequestValidationError(
                [
                    {
                        "type": "value_error",
                        "loc": ("body", "product_price_id"),
                        "msg": "Price does not exist.",
                        "input": checkout_create.product_price_id,
                    }
                ]
            )

        if price.is_archived:
            raise PolarRequestValidationError(
                [
                    {
                        "type": "value_error",
                        "loc": ("body", "product_price_id"),
                        "msg": "Price is archived.",
                        "input": checkout_create.product_price_id,
                    }
                ]
            )

        product = price.product
        if product.is_archived:
            raise PolarRequestValidationError(
                [
                    {
                        "type": "value_error",
                        "loc": ("body", "product_price_id"),
                        "msg": "Product is archived.",
                        "input": checkout_create.product_price_id,
                    }
                ]
            )

        product = await self._eager_load_product(session, product)

        amount = None
        currency = None
        if isinstance(price, ProductPriceFixed):
            amount = price.price_amount
            currency = price.price_currency
        elif isinstance(price, ProductPriceCustom):
            currency = price.price_currency
            if amount is None:
                amount = price.preset_amount or 1000
        elif isinstance(price, ProductPriceFree):
            amount = None
            currency = None

        checkout = Checkout(
            payment_processor=PaymentProcessor.stripe,
            client_secret=generate_token(prefix=CHECKOUT_CLIENT_SECRET_PREFIX),
            amount=amount,
            currency=currency,
            product=product,
            product_price=price,
        )
        if is_direct_user(auth_subject):
            checkout.customer = auth_subject.subject
            checkout.customer_email = auth_subject.subject.email
        elif checkout_create.customer_email is not None:
            checkout.customer_email = checkout_create.customer_email

        if checkout.payment_processor == PaymentProcessor.stripe:
            if checkout.customer and checkout.customer.stripe_customer_id is not None:
                stripe_customer_session = await stripe_service.create_customer_session(
                    checkout.customer.stripe_customer_id
                )
                checkout.payment_processor_metadata = {
                    **(checkout.payment_processor_metadata or {}),
                    "customer_session_client_secret": stripe_customer_session.client_secret,
                }

        checkout.customer_ip_address = ip_address
        checkout = await self._update_checkout_ip_geolocation(
            session, checkout, ip_geolocation_client
        )

        try:
            checkout = await self._update_checkout_tax(session, checkout)
        # Swallow incomplete tax calculation error: require it only on confirm
        except TaxCalculationError:
            pass

        session.add(checkout)

        await session.flush()
        await self._after_checkout_created(session, checkout)

        # Send a depreciation event to the organization's members
        if checkout_create.from_legacy_checkout_link:
            user_organizations = await user_organization_service.list_by_org(
                session, product.organization_id
            )
            for user_organization in user_organizations:
                enqueue_job(
                    "loops.send_event",
                    user_organization.user.email,
                    "deprecated_checkout_link_v2",
                    event_properties={
                        "product": product.name,
                        "product_price_id": str(price.id),
                        "organization": product.organization.name,
                    },
                )

        return checkout

    async def checkout_link_create(
        self,
        session: AsyncSession,
        checkout_link: CheckoutLink,
        embed_origin: str | None = None,
        ip_geolocation_client: ip_geolocation.IPGeolocationClient | None = None,
        ip_address: str | None = None,
    ) -> Checkout:
        price = checkout_link.product_price

        if price.is_archived:
            raise PolarRequestValidationError(
                [
                    {
                        "type": "value_error",
                        "loc": ("body", "product_price_id"),
                        "msg": "Price is archived.",
                        "input": price.id,
                    }
                ]
            )

        product = price.product
        if product.is_archived:
            raise PolarRequestValidationError(
                [
                    {
                        "type": "value_error",
                        "loc": ("body", "product_price_id"),
                        "msg": "Product is archived.",
                        "input": price.id,
                    }
                ]
            )

        product = await self._eager_load_product(session, product)

        amount = None
        currency = None
        if isinstance(price, ProductPriceFixed):
            amount = price.price_amount
            currency = price.price_currency
        elif isinstance(price, ProductPriceCustom):
            currency = price.price_currency
            if amount is None:
                amount = price.preset_amount or 1000
        elif isinstance(price, ProductPriceFree):
            amount = None
            currency = None

        checkout = Checkout(
            client_secret=generate_token(prefix=CHECKOUT_CLIENT_SECRET_PREFIX),
            amount=amount,
            currency=currency,
            product=product,
            product_price=price,
            embed_origin=embed_origin,
            customer_ip_address=ip_address,
            payment_processor=checkout_link.payment_processor,
            success_url=checkout_link.success_url,
            user_metadata=checkout_link.user_metadata,
        )
        session.add(checkout)

        checkout = await self._update_checkout_ip_geolocation(
            session, checkout, ip_geolocation_client
        )

        try:
            checkout = await self._update_checkout_tax(session, checkout)
        # Swallow incomplete tax calculation error: require it only on confirm
        except TaxCalculationError:
            pass

        await session.flush()
        await self._after_checkout_created(session, checkout)

        return checkout

    async def update(
        self,
        session: AsyncSession,
        checkout: Checkout,
        checkout_update: CheckoutUpdate | CheckoutUpdatePublic,
        ip_geolocation_client: ip_geolocation.IPGeolocationClient | None = None,
    ) -> Checkout:
        checkout = await self._update_checkout(
            session, checkout, checkout_update, ip_geolocation_client
        )
        try:
            checkout = await self._update_checkout_tax(session, checkout)
        # Swallow incomplete tax calculation error: require it only on confirm
        except TaxCalculationError:
            pass

        await self._after_checkout_updated(session, checkout)
        return checkout

    async def confirm(
        self,
        session: AsyncSession,
        checkout: Checkout,
        checkout_confirm: CheckoutConfirm,
    ) -> Checkout:
        checkout = await self._update_checkout(session, checkout, checkout_confirm)

        errors: list[ValidationError] = []
        try:
            checkout = await self._update_checkout_tax(session, checkout)
        except TaxCalculationError as e:
            errors.append(
                {
                    "type": "value_error",
                    "loc": ("body", "customer_billing_address"),
                    "msg": e.message,
                    "input": None,
                }
            )

        if checkout.amount is None and isinstance(
            checkout.product_price, ProductPriceCustom
        ):
            errors.append(
                {
                    "type": "missing",
                    "loc": ("body", "amount"),
                    "msg": "Amount is required for custom prices.",
                    "input": None,
                }
            )

        for required_field in self._get_required_confirm_fields(checkout):
            if getattr(checkout, required_field) is None:
                errors.append(
                    {
                        "type": "missing",
                        "loc": ("body", required_field),
                        "msg": "Field is required.",
                        "input": None,
                    }
                )

        if (
            checkout.is_payment_required
            and checkout_confirm.confirmation_token_id is None
        ):
            errors.append(
                {
                    "type": "missing",
                    "loc": ("body", "confirmation_token_id"),
                    "msg": "Confirmation token is required.",
                    "input": None,
                }
            )

        if len(errors) > 0:
            raise PolarRequestValidationError(errors)

        assert checkout.customer_email is not None

        if checkout.payment_processor == PaymentProcessor.stripe:
            stripe_customer_id = await self._create_or_update_stripe_customer(
                session, checkout
            )
            checkout.payment_processor_metadata = {"customer_id": stripe_customer_id}

            if checkout.is_payment_required:
                assert checkout_confirm.confirmation_token_id is not None
                assert checkout.customer_billing_address is not None
                payment_intent_metadata: dict[str, str] = {
                    "checkout_id": str(checkout.id),
                    "type": ProductType.product,
                    "tax_amount": str(checkout.tax_amount),
                    "tax_country": checkout.customer_billing_address.country,
                }
                if (
                    state := checkout.customer_billing_address.get_unprefixed_state()
                ) is not None:
                    payment_intent_metadata["tax_state"] = state
                payment_intent_params: stripe_lib.PaymentIntent.CreateParams = {
                    "amount": checkout.total_amount or 0,
                    "currency": checkout.currency or "usd",
                    "automatic_payment_methods": {"enabled": True},
                    "confirm": True,
                    "confirmation_token": checkout_confirm.confirmation_token_id,
                    "customer": stripe_customer_id,
                    "metadata": payment_intent_metadata,
                    "return_url": settings.generate_frontend_url(
                        f"/checkout/{checkout.client_secret}/confirmation"
                    ),
                }
                if checkout.product_price.is_recurring:
                    payment_intent_params["setup_future_usage"] = "off_session"

                try:
                    payment_intent = await stripe_service.create_payment_intent(
                        **payment_intent_params
                    )
                except stripe_lib.StripeError as e:
                    error = e.error
                    error_type = error.type if error is not None else None
                    error_message = error.message if error is not None else None
                    raise PaymentError(checkout, error_type, error_message)

                checkout.payment_processor_metadata = {
                    **checkout.payment_processor_metadata,
                    "payment_intent_client_secret": payment_intent.client_secret,
                    "payment_intent_status": payment_intent.status,
                }

        if not checkout.is_payment_required:
            enqueue_job("checkout.handle_free_success", checkout_id=checkout.id)

        checkout.status = CheckoutStatus.confirmed
        session.add(checkout)

        await self._after_checkout_updated(session, checkout)

        return checkout

    async def handle_stripe_success(
        self,
        session: AsyncSession,
        checkout_id: uuid.UUID,
        payment_intent: stripe_lib.PaymentIntent,
    ) -> Checkout:
        checkout = await self._get_eager_loaded_checkout(session, checkout_id)

        if checkout is None:
            raise CheckoutDoesNotExist(checkout_id)

        if checkout.status != CheckoutStatus.confirmed:
            raise NotConfirmedCheckout(checkout)

        if payment_intent.status != "succeeded":
            raise PaymentIntentNotSucceeded(checkout, payment_intent.id)

        if payment_intent.customer is None:
            raise NoCustomerOnPaymentIntent(checkout, payment_intent.id)

        if payment_intent.payment_method is None:
            raise NoPaymentMethodOnPaymentIntent(checkout, payment_intent.id)

        stripe_customer_id = get_expandable_id(payment_intent.customer)
        stripe_payment_method_id = get_expandable_id(payment_intent.payment_method)
        product_price = checkout.product_price
        metadata = {
            "type": ProductType.product,
            "product_id": str(checkout.product_id),
            "product_price_id": str(checkout.product_price_id),
            "checkout_id": str(checkout.id),
        }
        idempotency_key = f"checkout_{checkout.id}"

        stripe_price_id = product_price.stripe_price_id
        # For pay-what-you-want prices, we need to generate a dedicated price in Stripe
        if isinstance(product_price, ProductPriceCustom):
            assert checkout.amount is not None
            assert checkout.currency is not None
            assert checkout.product.stripe_product_id is not None
            price_params: stripe_lib.Price.CreateParams = {
                "unit_amount": checkout.amount,
                "currency": checkout.currency,
                "metadata": {
                    "product_price_id": str(checkout.product_price_id),
                },
            }
            if product_price.is_recurring:
                price_params["recurring"] = {
                    "interval": product_price.recurring_interval.as_literal(),
                }
            stripe_custom_price = await stripe_service.create_price_for_product(
                checkout.product.stripe_product_id,
                price_params,
                idempotency_key=f"{idempotency_key}_price",
            )
            stripe_price_id = stripe_custom_price.id

        if product_price.is_recurring:
            subscription = checkout.subscription
            # New subscription
            if subscription is None:
                (
                    stripe_subscription,
                    stripe_invoice,
                ) = await stripe_service.create_out_of_band_subscription(
                    customer=stripe_customer_id,
                    currency=checkout.currency or "usd",
                    price=stripe_price_id,
                    automatic_tax=checkout.product.is_tax_applicable,
                    metadata=metadata,
                    invoice_metadata={
                        "payment_intent_id": payment_intent.id,
                        "checkout_id": str(checkout.id),
                    },
                    idempotency_key=idempotency_key,
                )
            # Subscription upgrade
            else:
                assert subscription.stripe_subscription_id is not None
                await session.refresh(subscription, {"price"})
                (
                    stripe_subscription,
                    stripe_invoice,
                ) = await stripe_service.update_out_of_band_subscription(
                    subscription_id=subscription.stripe_subscription_id,
                    old_price=subscription.price.stripe_price_id,
                    new_price=stripe_price_id,
                    automatic_tax=checkout.product.is_tax_applicable,
                    metadata=metadata,
                    invoice_metadata={
                        "payment_intent_id": payment_intent.id,
                        "checkout_id": str(checkout.id),
                    },
                    idempotency_key=idempotency_key,
                )
            await stripe_service.set_automatically_charged_subscription(
                stripe_subscription.id,
                stripe_payment_method_id,
                idempotency_key=f"{idempotency_key}_subscription_auto_charge",
            )
        else:
            stripe_invoice = await stripe_service.create_out_of_band_invoice(
                customer=stripe_customer_id,
                currency=checkout.currency or "usd",
                price=stripe_price_id,
                automatic_tax=checkout.product.is_tax_applicable,
                metadata={
                    **metadata,
                    "payment_intent_id": payment_intent.id,
                },
                idempotency_key=idempotency_key,
            )

        # Sanity check to make sure we didn't mess up the amount.
        # Don't raise an error so the order can be successfully completed nonetheless.
        if stripe_invoice.total != payment_intent.amount:
            log.error(
                "Mismatch between payment intent and invoice amount",
                checkout=checkout.id,
                payment_intent=payment_intent.id,
                invoice=stripe_invoice.id,
            )

        checkout.status = CheckoutStatus.succeeded
        session.add(checkout)

        await self._after_checkout_updated(session, checkout)

        return checkout

    async def handle_stripe_failure(
        self,
        session: AsyncSession,
        checkout_id: uuid.UUID,
        payment_intent: stripe_lib.PaymentIntent,
    ) -> Checkout:
        checkout = await self._get_eager_loaded_checkout(session, checkout_id)

        if checkout is None:
            raise CheckoutDoesNotExist(checkout_id)

        # Checkout is not confirmed: do nothing
        # This is the case of an immediate failure, e.g. card declined
        # In this case, the checkout is still open and the user can retry
        if checkout.status != CheckoutStatus.confirmed:
            return checkout

        checkout.status = CheckoutStatus.failed
        session.add(checkout)

        await self._after_checkout_updated(session, checkout)

        return checkout

    async def handle_free_success(
        self, session: AsyncSession, checkout_id: uuid.UUID
    ) -> Checkout:
        checkout = await self._get_eager_loaded_checkout(session, checkout_id)

        if checkout is None:
            raise CheckoutDoesNotExist(checkout_id)

        if checkout.status != CheckoutStatus.confirmed:
            raise NotConfirmedCheckout(checkout)

        stripe_customer_id = checkout.payment_processor_metadata.get("customer_id")
        if stripe_customer_id is None:
            raise NoCustomerOnCheckout(checkout)

        product_price = checkout.product_price
        if not isinstance(product_price, ProductPriceFree):
            raise NotAFreePrice(checkout)

        stripe_price_id = product_price.stripe_price_id
        metadata = {
            "type": ProductType.product,
            "product_id": str(checkout.product_id),
            "product_price_id": str(checkout.product_price_id),
            "checkout_id": str(checkout.id),
        }
        idempotency_key = f"checkout_{checkout.id}"

        if product_price.is_recurring:
            (
                stripe_subscription,
                _,
            ) = await stripe_service.create_out_of_band_subscription(
                customer=stripe_customer_id,
                currency=checkout.currency or "usd",
                price=stripe_price_id,
                automatic_tax=False,
                metadata=metadata,
                idempotency_key=idempotency_key,
            )
            await stripe_service.set_automatically_charged_subscription(
                stripe_subscription.id,
                None,
                idempotency_key=f"{idempotency_key}_subscription_auto_charge",
            )
        else:
            await stripe_service.create_out_of_band_invoice(
                customer=stripe_customer_id,
                currency=checkout.currency or "usd",
                price=stripe_price_id,
                automatic_tax=False,
                metadata=metadata,
                idempotency_key=idempotency_key,
            )

        checkout.status = CheckoutStatus.succeeded
        session.add(checkout)

        await self._after_checkout_updated(session, checkout)

        return checkout

    async def get_by_client_secret(
        self, session: AsyncSession, client_secret: str
    ) -> Checkout | None:
        statement = (
            select(Checkout)
            .where(
                Checkout.deleted_at.is_(None),
                Checkout.expires_at > utc_now(),
                Checkout.client_secret == client_secret,
            )
            .join(Checkout.product)
            .options(
                contains_eager(Checkout.product).options(
                    joinedload(Product.organization),
                    selectinload(Product.product_medias),
                    selectinload(Product.attached_custom_fields),
                )
            )
        )
        result = await session.execute(statement)
        return result.unique().scalar_one_or_none()

    async def expire_open_checkouts(self, session: AsyncSession) -> None:
        statement = (
            update(Checkout)
            .where(
                Checkout.deleted_at.is_(None),
                Checkout.expires_at <= utc_now(),
                Checkout.status == CheckoutStatus.open,
            )
            .values(status=CheckoutStatus.expired)
        )
        await session.execute(statement)

    async def _get_upgradable_subscription(
        self, session: AsyncSession, id: uuid.UUID, organization_id: uuid.UUID
    ) -> Subscription | None:
        statement = (
            select(Subscription)
            .join(Product)
            .where(
                Subscription.id == id,
                Product.organization_id == organization_id,
            )
            .options(
                contains_eager(Subscription.product),
                joinedload(Subscription.price),
                joinedload(Subscription.user),
            )
        )
        result = await session.execute(statement)
        return result.scalars().unique().one_or_none()

    async def _update_checkout(
        self,
        session: AsyncSession,
        checkout: Checkout,
        checkout_update: CheckoutUpdate | CheckoutUpdatePublic,
        ip_geolocation_client: ip_geolocation.IPGeolocationClient | None = None,
    ) -> Checkout:
        if checkout.status != CheckoutStatus.open:
            raise NotOpenCheckout(checkout)

        if checkout_update.product_price_id is not None:
            price = await product_price_service.get_by_id(
                session, checkout_update.product_price_id
            )
            if (
                price is None
                or price.product.organization_id != checkout.product.organization_id
            ):
                raise PolarRequestValidationError(
                    [
                        {
                            "type": "value_error",
                            "loc": ("body", "product_price_id"),
                            "msg": "Price does not exist.",
                            "input": checkout_update.product_price_id,
                        }
                    ]
                )

            if price.is_archived:
                raise PolarRequestValidationError(
                    [
                        {
                            "type": "value_error",
                            "loc": ("body", "product_price_id"),
                            "msg": "Price is archived.",
                            "input": checkout_update.product_price_id,
                        }
                    ]
                )

            if price.product_id != checkout.product_id:
                raise PolarRequestValidationError(
                    [
                        {
                            "type": "value_error",
                            "loc": ("body", "product_price_id"),
                            "msg": "Price does not belong to the product.",
                            "input": checkout_update.product_price_id,
                        }
                    ]
                )

            checkout.product_price = price
            if isinstance(price, ProductPriceFixed):
                checkout.amount = price.price_amount
                checkout.currency = price.price_currency
            elif isinstance(price, ProductPriceCustom):
                checkout.currency = price.price_currency
            elif isinstance(price, ProductPriceFree):
                checkout.amount = None
                checkout.currency = None

        price = checkout.product_price
        if checkout_update.amount is not None and isinstance(price, ProductPriceCustom):
            if (
                price.minimum_amount is not None
                and checkout_update.amount < price.minimum_amount
            ):
                raise PolarRequestValidationError(
                    [
                        {
                            "type": "greater_than_equal",
                            "loc": ("body", "amount"),
                            "msg": "Amount is below minimum.",
                            "input": checkout_update.amount,
                            "ctx": {"ge": price.minimum_amount},
                        }
                    ]
                )
            elif (
                price.maximum_amount is not None
                and checkout_update.amount > price.maximum_amount
            ):
                raise PolarRequestValidationError(
                    [
                        {
                            "type": "less_than_equal",
                            "loc": ("body", "amount"),
                            "msg": "Amount is above maximum.",
                            "input": checkout_update.amount,
                            "ctx": {"le": price.maximum_amount},
                        }
                    ]
                )

            checkout.amount = checkout_update.amount

        if checkout_update.customer_billing_address:
            checkout.customer_billing_address = checkout_update.customer_billing_address

        if (
            checkout_update.customer_tax_id is None
            and "customer_tax_id" in checkout_update.model_fields_set
        ):
            checkout.customer_tax_id = None
        else:
            customer_tax_id_number = (
                checkout_update.customer_tax_id or checkout.customer_tax_id_number
            )
            if customer_tax_id_number is not None:
                customer_billing_address = (
                    checkout_update.customer_billing_address
                    or checkout.customer_billing_address
                )
                if customer_billing_address is None:
                    raise PolarRequestValidationError(
                        [
                            {
                                "type": "missing",
                                "loc": ("body", "customer_billing_address"),
                                "msg": "Country is required to validate tax ID.",
                                "input": None,
                            }
                        ]
                    )
                try:
                    checkout.customer_tax_id = validate_tax_id(
                        customer_tax_id_number, customer_billing_address.country
                    )
                except ValueError as e:
                    raise PolarRequestValidationError(
                        [
                            {
                                "type": "value_error",
                                "loc": ("body", "customer_tax_id"),
                                "msg": "Invalid tax ID.",
                                "input": customer_tax_id_number,
                            }
                        ]
                    ) from e

        if checkout_update.custom_field_data:
            custom_field_data = validate_custom_field_data(
                checkout.product.attached_custom_fields,
                checkout_update.custom_field_data,
            )
            checkout.custom_field_data = custom_field_data

        checkout = await self._update_checkout_ip_geolocation(
            session, checkout, ip_geolocation_client
        )

        exclude = {
            "product_price_id",
            "amount",
            "customer_billing_address",
            "customer_tax_id",
            "custom_field_data",
        }

        if checkout.customer_id is not None:
            exclude.add("customer_email")

        for attr, value in checkout_update.model_dump(
            exclude_unset=True, exclude=exclude, by_alias=True
        ).items():
            setattr(checkout, attr, value)

        session.add(checkout)
        return checkout

    async def _update_checkout_tax(
        self, session: AsyncSession, checkout: Checkout
    ) -> Checkout:
        if not checkout.product.is_tax_applicable:
            checkout.tax_amount = 0
            return checkout

        if (
            checkout.currency is not None
            and checkout.amount is not None
            and checkout.customer_billing_address is not None
            and checkout.product.stripe_product_id is not None
        ):
            try:
                tax_amount = await calculate_tax(
                    checkout.currency,
                    checkout.amount,
                    checkout.product.stripe_product_id,
                    checkout.customer_billing_address,
                    [checkout.customer_tax_id]
                    if checkout.customer_tax_id is not None
                    else [],
                )
                checkout.tax_amount = tax_amount
            except TaxCalculationError:
                checkout.tax_amount = None
                raise
            finally:
                session.add(checkout)

        return checkout

    async def _update_checkout_ip_geolocation(
        self,
        session: AsyncSession,
        checkout: Checkout,
        ip_geolocation_client: ip_geolocation.IPGeolocationClient | None,
    ) -> Checkout:
        if ip_geolocation_client is None:
            return checkout

        if checkout.customer_ip_address is None:
            return checkout

        if checkout.customer_billing_address is not None:
            return checkout

        country = ip_geolocation.get_ip_country(
            ip_geolocation_client, checkout.customer_ip_address
        )
        if country is not None:
            checkout.customer_billing_address = Address.model_validate(
                {"country": country}
            )
            session.add(checkout)

        return checkout

    def _get_required_confirm_fields(self, checkout: Checkout) -> set[str]:
        fields = {"customer_email"}
        if checkout.is_payment_required:
            fields.update({"customer_name", "customer_billing_address"})
        return fields

    async def _create_or_update_stripe_customer(
        self, session: AsyncSession, checkout: Checkout
    ) -> str:
        assert checkout.customer_email is not None

        stripe_customer_id: str | None = None
        if checkout.customer_id is not None:
            user = await user_service.get(session, checkout.customer_id)
            if user is not None and user.stripe_customer_id is not None:
                stripe_customer_id = user.stripe_customer_id

        if stripe_customer_id is None:
            create_params: stripe_lib.Customer.CreateParams = {
                "email": checkout.customer_email
            }
            if checkout.customer_name is not None:
                create_params["name"] = checkout.customer_name
            if checkout.customer_billing_address is not None:
                create_params["address"] = checkout.customer_billing_address.to_dict()  # type: ignore
            if checkout.customer_tax_id is not None:
                create_params["tax_id_data"] = [
                    to_stripe_tax_id(checkout.customer_tax_id)
                ]
            stripe_customer = await stripe_service.create_customer(**create_params)
            stripe_customer_id = stripe_customer.id
        else:
            update_params: stripe_lib.Customer.ModifyParams = {
                "email": checkout.customer_email
            }
            if checkout.customer_name is not None:
                update_params["name"] = checkout.customer_name
            if checkout.customer_billing_address is not None:
                update_params["address"] = checkout.customer_billing_address.to_dict()  # type: ignore
            await stripe_service.update_customer(
                stripe_customer_id,
                tax_id=to_stripe_tax_id(checkout.customer_tax_id)
                if checkout.customer_tax_id is not None
                else None,
                **update_params,
            )

        return stripe_customer_id

    async def _get_eager_loaded_checkout(
        self, session: AsyncSession, checkout_id: uuid.UUID
    ) -> Checkout | None:
        return await self.get(
            session,
            checkout_id,
            options=(
                joinedload(Checkout.product).options(
                    selectinload(Product.product_medias),
                    selectinload(Product.attached_custom_fields),
                ),
                joinedload(Checkout.product_price),
            ),
        )

    async def _after_checkout_created(
        self, session: AsyncSession, checkout: Checkout
    ) -> None:
        organization = await organization_service.get(
            session, checkout.product.organization_id
        )
        assert organization is not None
        await webhook_service.send(
            session, organization, (WebhookEventType.checkout_created, checkout)
        )

    async def _after_checkout_updated(
        self, session: AsyncSession, checkout: Checkout
    ) -> None:
        await publish(
            "checkout.updated", {}, checkout_client_secret=checkout.client_secret
        )
        organization = await organization_service.get(
            session, checkout.product.organization_id
        )
        assert organization is not None
        await webhook_service.send(
            session, organization, (WebhookEventType.checkout_updated, checkout)
        )

    async def _eager_load_product(
        self, session: AsyncSession, product: Product
    ) -> Product:
        await session.refresh(
            product,
            {"organization", "prices", "product_medias", "attached_custom_fields"},
        )
        return product

    def _get_readable_checkout_statement(
        self, auth_subject: AuthSubject[User | Organization]
    ) -> Select[tuple[Checkout]]:
        statement = (
            select(Checkout)
            .where(Checkout.deleted_at.is_(None))
            .join(Checkout.product)
            .options(
                contains_eager(Checkout.product).options(
                    joinedload(Product.product_medias),
                    joinedload(Product.attached_custom_fields),
                )
            )
        )

        if is_user(auth_subject):
            user = auth_subject.subject
            statement = statement.where(
                Product.organization_id.in_(
                    select(UserOrganization.organization_id).where(
                        UserOrganization.user_id == user.id,
                        UserOrganization.deleted_at.is_(None),
                    )
                )
            )
        elif is_organization(auth_subject):
            statement = statement.where(
                Product.organization_id == auth_subject.subject.id,
            )

        return statement


checkout = CheckoutService(Checkout)
