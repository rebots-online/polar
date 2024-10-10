"""Add Checkout model

Revision ID: 5f981f48beef
Revises: 19f9bb88313b
Create Date: 2024-09-24 13:35:01.715400

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Polar Custom Imports
import polar.kit.address

# revision identifiers, used by Alembic.
revision = "5f981f48beef"
down_revision = "19f9bb88313b"
branch_labels: tuple[str] | None = None
depends_on: tuple[str] | None = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "checkouts",
        sa.Column("payment_processor", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("client_secret", sa.String(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "user_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "payment_processor_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("amount", sa.Integer(), nullable=True),
        sa.Column("tax_amount", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("product_price_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column("customer_name", sa.String(), nullable=True),
        sa.Column("customer_email", sa.String(), nullable=True),
        sa.Column("customer_ip_address", sa.String(), nullable=True),
        sa.Column(
            "customer_billing_address",
            polar.kit.address.AddressType(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("modified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["users.id"],
            name=op.f("checkouts_customer_id_fkey"),
            ondelete="cascade",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("checkouts_product_id_fkey"),
            ondelete="cascade",
        ),
        sa.ForeignKeyConstraint(
            ["product_price_id"],
            ["product_prices.id"],
            name=op.f("checkouts_product_price_id_fkey"),
            ondelete="cascade",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("checkouts_pkey")),
    )
    op.create_index(
        op.f("ix_checkouts_payment_processor"),
        "checkouts",
        ["payment_processor"],
        unique=False,
    )
    op.create_index(op.f("ix_checkouts_status"), "checkouts", ["status"], unique=False)
    op.create_index(
        op.f("ix_checkouts_client_secret"), "checkouts", ["client_secret"], unique=True
    )
    op.create_index(
        op.f("ix_checkouts_created_at"), "checkouts", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_checkouts_deleted_at"), "checkouts", ["deleted_at"], unique=False
    )
    op.create_index(
        op.f("ix_checkouts_modified_at"), "checkouts", ["modified_at"], unique=False
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_checkouts_modified_at"), table_name="checkouts")
    op.drop_index(op.f("ix_checkouts_deleted_at"), table_name="checkouts")
    op.drop_index(op.f("ix_checkouts_created_at"), table_name="checkouts")
    op.drop_index(op.f("ix_checkouts_client_secret"), table_name="checkouts")
    op.drop_index(op.f("ix_checkouts_status"), table_name="checkouts")
    op.drop_index(op.f("ix_checkouts_payment_processor"), table_name="checkouts")
    op.drop_table("checkouts")
    # ### end Alembic commands ###